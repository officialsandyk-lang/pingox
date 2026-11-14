// Runtime configuration is local and operator-controlled. No public admin API.
use std::sync::RwLock;
use std::io::Read;

fn default_weight() -> usize { 1 }
fn default_revision() -> u64 { 1 }
fn default_port() -> u16 { 8080 }
fn default_balancing() -> String { "round_robin".into() }

impl Default for SecurityConfig {
    fn default() -> Self {
        Self { blocked_paths: vec!["/.env".into(), "/.git".into()],
            allowed_methods: vec!["GET".into(), "HEAD".into(), "POST".into()],
            rate_limit_per_minute: 600, max_connections: 1000,
            max_request_body_bytes: 1048576, upstream_timeout_seconds: 30,
            require_api_key: false, failure_threshold: 3, failure_cooldown_seconds: 10 }
    }
}

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct WireRoute {
    path: String,
    upstreams: Vec<UpstreamConfig>,
    #[serde(default = "default_balancing")]
    balancing: String,
}

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct WireConfig {
    #[serde(default = "default_revision")]
    revision: u64,
    #[serde(default = "default_port")]
    port: u16,
    routes: Vec<WireRoute>,
    #[serde(default)]
    security: SecurityConfig,
}

fn checked_path(path: &str) -> bool {
    path.starts_with('/') && !path.contains("//") && (path == "/" || !path.ends_with('/'))
        && path.bytes().all(|b| b.is_ascii_alphanumeric() || b"/_-.".contains(&b))
        && !path.split('/').any(|part| part == "." || part == "..")
}

fn checked_host(host: &str) -> bool {
    !host.is_empty() && host.as_bytes()[0].is_ascii_alphanumeric()
        && host.as_bytes()[host.len()-1].is_ascii_alphanumeric()
        && host.bytes().all(|b| b.is_ascii_alphanumeric() || b".-".contains(&b))
}

fn decode_config(bytes: &[u8], running_port: Option<u16>) -> std::result::Result<WireConfig, String> {
    let config: WireConfig = serde_json::from_slice(bytes).map_err(|_| "invalid configuration schema")?;
    if config.revision == 0 || config.revision > 9007199254740991 || config.port < 1024 {
        return Err("invalid revision or listener port".into());
    }
    if running_port.is_some_and(|port| port != config.port) {
        return Err("listener port changes require a restart".into());
    }
    if config.routes.is_empty() || config.routes.len() > 1024 { return Err("invalid route count".into()); }
    let mut paths = std::collections::HashSet::new();
    for route in &config.routes {
        if !checked_path(&route.path) || route.path == "/_gateway" || route.path.starts_with("/_gateway/")
            || !paths.insert(&route.path) { return Err("invalid or duplicate route path".into()); }
        if !["round_robin", "weighted_round_robin", "least_connections", "random", "ip_hash"].contains(&route.balancing.as_str())
            || route.upstreams.is_empty() || route.upstreams.len() > 128 { return Err("invalid upstream pool".into()); }
        let mut addresses = std::collections::HashSet::new();
        for upstream in &route.upstreams {
            let (host, port) = upstream.address.rsplit_once(':').ok_or("invalid upstream address")?;
            if !checked_host(host) || host == "0.0.0.0" || port.is_empty() || port.len() > 5
                || !port.bytes().all(|b| b.is_ascii_digit()) || port.parse::<u16>().map_or(true, |p| p == 0)
                || !addresses.insert(&upstream.address) || upstream.weight == 0 || upstream.weight > 1000
                || (route.balancing != "weighted_round_robin" && upstream.weight != 1) {
                return Err("invalid or duplicate upstream".into());
            }
            if (upstream.tls && (!checked_host(&upstream.server_name) || upstream.server_name.len() > 253))
                || (!upstream.tls && !upstream.server_name.is_empty()) { return Err("invalid TLS server name".into()); }
        }
    }
    let s = &config.security;
    if s.blocked_paths.iter().any(|p| !checked_path(p)) || s.allowed_methods.is_empty()
        || s.allowed_methods.iter().any(|m| !["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"].contains(&m.as_str()))
        || s.rate_limit_per_minute > 10000000 || !(1..=100000).contains(&s.max_connections)
        || !(1..=1073741824).contains(&s.max_request_body_bytes) || !(1..=300).contains(&s.upstream_timeout_seconds)
        || !(1..=100).contains(&s.failure_threshold) || !(1..=300).contains(&s.failure_cooldown_seconds) {
        return Err("invalid security policy".into());
    }
    Ok(config)
}

fn read_config(path: &std::path::Path) -> std::result::Result<Vec<u8>, String> {
    let file = std::fs::File::open(path).map_err(|_| "cannot open runtime configuration")?;
    let mut bytes = Vec::new();
    file.take(4194305).read_to_end(&mut bytes).map_err(|_| "cannot read runtime configuration")?;
    if bytes.len() > 4194304 { return Err("runtime configuration exceeds 4 MiB".into()); }
    Ok(bytes)
}

fn configured_runtime(config: WireConfig) -> std::result::Result<Runtime, String> {
    let keys = load_api_keys(config.security.require_api_key)?;
    let routes = config.routes.into_iter().map(|r| RouteConfig {
        path: r.path, balancing: r.balancing, upstreams: r.upstreams,
        route_type: "proxy".into(), root: String::new(), index: String::new(),
    }).collect();
    let mut runtime = Runtime::with_keys(routes, config.security, keys);
    runtime.client_identity = load_client_identity()?;
    Ok(runtime)
}

struct Snapshot { revision: u64, runtime: Arc<Runtime> }
struct Control { current: RwLock<Snapshot>, rejected: AtomicUsize }
#[derive(Clone)]
struct GeneratedProxy { control: Arc<Control> }

impl GeneratedProxy {
    fn snapshot(ctx: &RequestContext) -> Arc<Runtime> { ctx.snapshot.as_ref().expect("request snapshot").clone() }
}

#[async_trait]
impl ProxyHttp for GeneratedProxy {
    type CTX = RequestContext;
    fn new_ctx(&self) -> RequestContext {
        let runtime = self.control.current.read().expect("configuration lock").runtime.clone();
        let mut ctx = runtime.new_ctx();
        ctx.snapshot = Some(runtime);
        ctx
    }
    async fn request_filter(&self, session: &mut Session, ctx: &mut RequestContext) -> Result<bool> {
        if session.req_header().uri.path() == "/_gateway/config" && session.req_header().method.as_str() == "GET" {
            let revision = self.control.current.read().expect("configuration lock").revision;
            let body = serde_json::to_vec(&serde_json::json!({"revision": revision,
                "rejected_updates": self.control.rejected.load(Ordering::Relaxed)})).unwrap();
            let mut header = ResponseHeader::build(200, None)?;
            header.insert_header("content-type", "application/json")?;
            header.insert_header("content-length", body.len().to_string())?;
            session.write_response_header(Box::new(header), false).await?;
            session.write_response_body(Some(Bytes::from(body)), true).await?;
            return Ok(true);
        }
        Self::snapshot(ctx).request_filter(session, ctx).await
    }
    async fn upstream_request_filter(&self, session: &mut Session, request: &mut pingora::http::RequestHeader,
                                    ctx: &mut RequestContext) -> Result<()> {
        Self::snapshot(ctx).upstream_request_filter(session, request, ctx).await
    }
    async fn request_body_filter(&self, session: &mut Session, body: &mut Option<Bytes>, end: bool,
                                ctx: &mut RequestContext) -> Result<()> {
        Self::snapshot(ctx).request_body_filter(session, body, end, ctx).await
    }
    async fn upstream_peer(&self, session: &mut Session, ctx: &mut RequestContext) -> Result<Box<HttpPeer>> {
        Self::snapshot(ctx).upstream_peer(session, ctx).await
    }
    async fn logging(&self, session: &mut Session, error: Option<&Error>, ctx: &mut RequestContext) {
        Self::snapshot(ctx).logging(session, error, ctx).await;
    }
}

fn watch_config(path: PathBuf, control: Arc<Control>, port: u16, initial: Vec<u8>) {
    std::thread::spawn(move || {
        let mut previous = initial;
        let mut read_failed = false;
        loop {
            std::thread::sleep(Duration::from_secs(1));
            let bytes = match read_config(&path) {
                Ok(bytes) => { read_failed = false; bytes },
                Err(_) => {
                    if !read_failed { control.rejected.fetch_add(1, Ordering::Relaxed); eprintln!("configuration read failed; keeping last known good revision"); }
                    read_failed = true;
                    continue;
                }
            };
            if bytes == previous { continue; }
            previous = bytes.clone();
            let candidate = (|| -> std::result::Result<(u64, Runtime), String> {
                let config = decode_config(&bytes, Some(port))?;
                let revision = config.revision;
                if revision <= control.current.read().unwrap().revision { return Err("revision must increase".into()); }
                Ok((revision, configured_runtime(config)?))
            })();
            match candidate {
                Ok((revision, mut runtime)) => {
                    let mut state = control.current.write().unwrap();
                    // Admission, quotas and request counters span configuration generations.
                    let mut loads = (*runtime.upstream_loads).clone();
                    for (route_index, route) in runtime.routes.iter().enumerate() {
                        if let Some(old_route) = state.runtime.routes.iter().position(|old| old.path == route.path) {
                            for (backend_index, backend) in route.upstreams.iter().enumerate() {
                                if let Some(old_backend) = state.runtime.routes[old_route].upstreams.iter().position(|old|
                                    old.address == backend.address && old.tls == backend.tls && old.server_name == backend.server_name) {
                                    loads[route_index][backend_index] = state.runtime.upstream_loads[old_route][old_backend].clone();
                                }
                            }
                        }
                    }
                    runtime.upstream_loads = Arc::new(loads);
                    runtime.active = state.runtime.active.clone();
                    runtime.completed = state.runtime.completed.clone();
                    runtime.failed = state.runtime.failed.clone();
                    runtime.rate_limiter = state.runtime.rate_limiter.clone();
                    *state = Snapshot { revision, runtime: Arc::new(runtime) };
                    println!("{}", serde_json::json!({"event": "configuration_applied", "revision": revision}));
                }
                Err(reason) => {
                    control.rejected.fetch_add(1, Ordering::Relaxed);
                    eprintln!("configuration rejected: {}", reason);
                }
            }
        }
    });
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() == 2 && args[1] == "--version" { println!("Pingox 0.2.0"); return; }
    if args.len() == 3 && args[1] == "--check-config" {
        match read_config(std::path::Path::new(&args[2])).and_then(|b| decode_config(&b, None)) {
            Ok(_) => { println!("Configuration is valid"); return; },
            Err(reason) => { eprintln!("{}", reason); std::process::exit(1); }
        }
    }
    if args.len() != 1 { eprintln!("Usage: gateway [--version | --check-config FILE]"); std::process::exit(2); }
    let path = std::env::var_os("PINGOX_CONFIG_FILE").map(PathBuf::from);
    let (runtime, revision, port, bytes) = if let Some(path) = &path {
        let bytes = read_config(path).unwrap_or_else(|e| { eprintln!("{}", e); std::process::exit(1); });
        let config = decode_config(&bytes, None).unwrap_or_else(|e| { eprintln!("{}", e); std::process::exit(1); });
        let revision = config.revision;
        let port = config.port;
        let runtime = configured_runtime(config).unwrap_or_else(|e| { eprintln!("{}", e); std::process::exit(1); });
        (runtime, revision, port, bytes)
    } else { (Runtime::new(build_routes(), build_security()), 0, __PORT__, Vec::new()) };
    let control = Arc::new(Control { current: RwLock::new(Snapshot { revision, runtime: Arc::new(runtime) }), rejected: AtomicUsize::new(0) });
    if let Some(path) = path { watch_config(path, control.clone(), port, bytes); }
    let proxy = GeneratedProxy { control };
    let mut server = Server::new(None).unwrap();
    let conf = Arc::get_mut(&mut server.configuration).expect("exclusive startup configuration");
    conf.grace_period_seconds = Some(5);
    conf.graceful_shutdown_timeout_seconds = Some(30);
    conf.threads = 2;
    conf.max_retries = 1;
    conf.ca_file = std::env::var("PINGOX_CA_FILE").ok();
    server.bootstrap();
    let mut service = http_proxy_service(&server.configuration, proxy);
    let host: std::net::Ipv4Addr = std::env::var("GATEWAY_BIND_HOST").unwrap_or_else(|_| "0.0.0.0".into()).parse().expect("invalid GATEWAY_BIND_HOST");
    service.add_tcp(&format!("{}:{}", host, port));
    server.add_service(service);
    println!("Starting Pingox on port {}", port);
    server.run_forever();
}

#[derive(Clone, Debug)]
struct ApiCredential { id: String, key: Vec<u8>, routes: Vec<String>, methods: Vec<String>, quota: usize }
#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct ScopedCredential { id: String, key: String, routes: Vec<String>, methods: Vec<String>, rate_limit_per_minute: usize }
#[derive(serde::Deserialize)]
#[serde(untagged)]
enum CredentialInput { Legacy(String), Scoped(ScopedCredential) }

fn parse_credentials(bytes: &[u8]) -> std::result::Result<Vec<ApiCredential>, String> {
    let inputs: Vec<CredentialInput> = serde_json::from_slice(bytes).map_err(|_| "invalid API credential schema")?;
    if inputs.is_empty() || inputs.len() > 64 { return Err("provide 1 to 64 API credentials".into()); }
    let mut ids = std::collections::HashSet::new();
    let mut keys = std::collections::HashSet::new();
    let mut result = Vec::new();
    for (index, input) in inputs.into_iter().enumerate() {
        let credential = match input {
            CredentialInput::Legacy(key) => ApiCredential { id: format!("legacy-{}", index), key: key.into_bytes(),
                routes: vec!["/".into()], methods: vec!["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"].into_iter().map(String::from).collect(), quota: 0 },
            CredentialInput::Scoped(c) => ApiCredential { id: c.id, key: c.key.into_bytes(), routes: c.routes, methods: c.methods, quota: c.rate_limit_per_minute },
        };
        if credential.key.len() != 64 || !credential.key.iter().all(|b| b.is_ascii_hexdigit())
            || credential.id.is_empty() || credential.id.len() > 64
            || !credential.id.bytes().all(|b| b.is_ascii_alphanumeric() || b"_.-".contains(&b))
            || !ids.insert(credential.id.clone()) || !keys.insert(credential.key.clone())
            || credential.routes.is_empty() || credential.routes.len() > 128 || credential.routes.iter().any(|p| !checked_path(p))
            || credential.methods.is_empty() || credential.methods.len() > 7
            || credential.methods.iter().any(|m| !["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"].contains(&m.as_str()))
            || credential.quota > 10000000 { return Err("invalid API credential policy".into()); }
        result.push(credential);
    }
    Ok(result)
}

fn load_client_identity() -> std::result::Result<Option<Arc<pingora::utils::tls::CertKey>>, String> {
    use rustls_pki_types::{CertificateDer, PrivateKeyDer, pem::PemObject};
    let cert = std::env::var_os("PINGOX_CLIENT_CERT_FILE");
    let key = std::env::var_os("PINGOX_CLIENT_KEY_FILE");
    match (cert, key) {
        (None, None) => Ok(None),
        (Some(cert), Some(key)) => {
            let cert = read_config(std::path::Path::new(&cert)).map_err(|_| "cannot read client certificate")?;
            let key = read_config(std::path::Path::new(&key)).map_err(|_| "cannot read client private key")?;
            let certs = CertificateDer::pem_slice_iter(&cert).map(|c| c.map(|c| c.as_ref().to_vec()))
                .collect::<std::result::Result<Vec<_>, _>>().map_err(|_| "invalid client certificate PEM")?;
            if certs.is_empty() { return Err("empty client certificate chain".into()); }
            let key = PrivateKeyDer::from_pem_slice(&key).map_err(|_| "invalid client private key PEM")?;
            if certs.iter().any(|cert| x509_parser::parse_x509_certificate(cert).is_err()) {
                return Err("invalid client certificate DER".into());
            }
            let validated = rustls::sign::CertifiedKey::from_der(certs.iter().cloned().map(CertificateDer::from).collect(),
                key.clone_key(), &rustls::crypto::ring::default_provider()).map_err(|_| "client certificate/key mismatch or invalid key")?;
            validated.keys_match().map_err(|_| "client certificate/key mismatch")?;
            Ok(Some(Arc::new(pingora::utils::tls::CertKey::new(certs, key.secret_der().to_vec()))))
        }
        _ => Err("provide both client certificate and private key files".into()),
    }
}
