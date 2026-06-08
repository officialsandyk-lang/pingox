use async_trait::async_trait;
use bytes::Bytes;
use pingora::http::ResponseHeader;
use pingora::prelude::*;
use std::collections::HashMap;
use std::env;
use std::io::{Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const GATEWAY_LISTEN_PORT: u16 = 8090;
const HEALTH_CHECK_PATH: &str = "/health";
const HEALTH_INTERVAL_SECONDS: u64 = 5;
const HEALTH_TIMEOUT_SECONDS: u64 = 2;
const HEALTHY_THRESHOLD: usize = 2;
const UNHEALTHY_THRESHOLD: usize = 3;

#[derive(Clone, Debug)]
struct UpstreamConfig {
    address: String,
    weight: usize,
}

#[derive(Clone, Debug)]
struct RouteConfig {
    path: String,
    route_type: String,
    root: String,
    index: String,
    balancing: String,
    upstreams: Vec<UpstreamConfig>,
}

#[derive(Clone, Debug)]
struct SecurityConfig {
    blocked_paths: Vec<String>,
    allowed_methods: Vec<String>,
    rate_limit_per_minute: usize,
    max_connections: usize,
    max_request_body_bytes: usize,
    upstream_timeout_seconds: u64,
}

#[derive(Clone, Debug)]
struct RuntimeUpstreamState {
    healthy: bool,
    consecutive_successes: usize,
    consecutive_failures: usize,
    last_status: Option<u16>,
    last_error: Option<String>,
    last_checked_unix_ms: u128,
}

#[derive(Debug)]
struct RateBucket {
    window_start: Instant,
    count: usize,
}

#[derive(Debug)]
struct RequestContext {}

#[derive(Clone)]
struct GeneratedProxy {
    routes: Arc<Vec<RouteConfig>>,
    counters: Arc<Vec<AtomicUsize>>,
    upstream_loads: Arc<Vec<Vec<AtomicUsize>>>,
    security: SecurityConfig,
    rate_limiter: Arc<Mutex<HashMap<String, RateBucket>>>,
    runtime_state: Arc<Mutex<HashMap<String, RuntimeUpstreamState>>>,
}

fn content_type_for_path(path: &PathBuf) -> &'static str {
    match path.extension().and_then(|value| value.to_str()).unwrap_or("") {
        "html" | "htm" => "text/html; charset=utf-8",
        "css" => "text/css; charset=utf-8",
        "js" => "application/javascript; charset=utf-8",
        "json" => "application/json; charset=utf-8",
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "gif" => "image/gif",
        "svg" => "image/svg+xml",
        "txt" => "text/plain; charset=utf-8",
        "ico" => "image/x-icon",
        _ => "application/octet-stream",
    }
}

fn current_unix_ms() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_else(|_| Duration::from_secs(0))
        .as_millis()
}

fn runtime_state_key(route_path: &str, upstream_address: &str) -> String {
    format!("{}|{}", route_path, upstream_address)
}

fn json_escape(value: &str) -> String {
    value
        .replace('\\', "\\\\")
        .replace('"', "\\\"")
        .replace('\n', "\\n")
        .replace('\r', "\\r")
        .replace('\t', "\\t")
}

fn safe_static_path(root: &str, relative_path: &str, index: &str) -> Option<PathBuf> {
    let mut path = PathBuf::from(root);

    let clean_relative = relative_path.trim_start_matches('/');

    if clean_relative.is_empty() {
        path.push(index);
        return Some(path);
    }

    for part in clean_relative.split('/') {
        if part.is_empty() {
            continue;
        }

        if part == "." || part == ".." || part.contains('\\') {
            return None;
        }

        path.push(part);
    }

    if relative_path.ends_with('/') {
        path.push(index);
    }

    Some(path)
}

impl GeneratedProxy {
    fn new(routes: Vec<RouteConfig>, security: SecurityConfig) -> Self {
        let counters = routes
            .iter()
            .map(|_| AtomicUsize::new(0))
            .collect::<Vec<_>>();

        let upstream_loads = routes
            .iter()
            .map(|route| {
                route
                    .upstreams
                    .iter()
                    .map(|_| AtomicUsize::new(0))
                    .collect::<Vec<_>>()
            })
            .collect::<Vec<_>>();

        let runtime_state = Arc::new(Mutex::new(HashMap::new()));

        {
            let mut state = runtime_state.lock().unwrap();

            for route in &routes {
                if route.route_type == "static" {
                    continue;
                }

                for upstream in &route.upstreams {
                    state.insert(
                        runtime_state_key(&route.path, &upstream.address),
                        RuntimeUpstreamState {
                            healthy: true,
                            consecutive_successes: 0,
                            consecutive_failures: 0,
                            last_status: None,
                            last_error: None,
                            last_checked_unix_ms: 0,
                        },
                    );
                }
            }
        }

        let proxy = Self {
            routes: Arc::new(routes),
            counters: Arc::new(counters),
            upstream_loads: Arc::new(upstream_loads),
            security,
            rate_limiter: Arc::new(Mutex::new(HashMap::new())),
            runtime_state,
        };

        proxy.start_health_checker();
        proxy
    }

    fn start_health_checker(&self) {
        let routes = self.routes.clone();
        let runtime_state = self.runtime_state.clone();

        thread::spawn(move || loop {
            for route in routes.iter() {
                if route.route_type == "static" {
                    continue;
                }

                for upstream in &route.upstreams {
                    let address = upstream.address.clone();
                    let route_path = route.path.clone();
                    let key = runtime_state_key(&route_path, &address);

                    let (healthy_now, last_status, last_error) =
                        check_upstream_health(&address, HEALTH_CHECK_PATH, HEALTH_TIMEOUT_SECONDS);

                    let mut state = match runtime_state.lock() {
                        Ok(guard) => guard,
                        Err(_) => continue,
                    };

                    let entry = state.entry(key).or_insert(RuntimeUpstreamState {
                        healthy: true,
                        consecutive_successes: 0,
                        consecutive_failures: 0,
                        last_status: None,
                        last_error: None,
                        last_checked_unix_ms: 0,
                    });

                    entry.last_checked_unix_ms = current_unix_ms();
                    entry.last_status = last_status;
                    entry.last_error = last_error;

                    if healthy_now {
                        entry.consecutive_successes += 1;
                        entry.consecutive_failures = 0;

                        if entry.consecutive_successes >= HEALTHY_THRESHOLD {
                            entry.healthy = true;
                        }
                    } else {
                        entry.consecutive_failures += 1;
                        entry.consecutive_successes = 0;

                        if entry.consecutive_failures >= UNHEALTHY_THRESHOLD {
                            entry.healthy = false;
                        }
                    }
                }
            }

            thread::sleep(Duration::from_secs(HEALTH_INTERVAL_SECONDS));
        });
    }

    fn match_route_index(&self, path: &str) -> Option<usize> {
        let mut best_match: Option<(usize, usize)> = None;

        for (index, route) in self.routes.iter().enumerate() {
            let route_path = route.path.as_str();

            let matched = if route_path == "/" {
                true
            } else {
                path == route_path || path.starts_with(&format!("{}/", route_path.trim_end_matches('/')))
            };

            if matched {
                let score = route_path.len();

                match best_match {
                    Some((_, best_score)) if best_score >= score => {}
                    _ => best_match = Some((index, score)),
                }
            }
        }

        best_match.map(|(index, _)| index)
    }

    async fn write_json_response(
        &self,
        session: &mut Session,
        status: u16,
        body: String,
    ) -> Result<bool> {
        let mut header = ResponseHeader::build(status, None)?;
        header.insert_header("content-type", "application/json; charset=utf-8")?;
        header.insert_header("cache-control", "private, no-store")?;
        header.insert_header("content-length", body.as_bytes().len().to_string())?;

        session.write_response_header(Box::new(header), false).await?;

        if session.req_header().method.as_str().eq_ignore_ascii_case("HEAD") {
            session.write_response_body(None, true).await?;
        } else {
            session
                .write_response_body(Some(Bytes::from(body)), true)
                .await?;
        }

        Ok(true)
    }

    async fn serve_runtime_state(&self, session: &mut Session) -> Result<bool> {
        let body = self.runtime_state_json();
        self.write_json_response(session, 200, body).await
    }

    fn runtime_state_json(&self) -> String {
        let state = match self.runtime_state.lock() {
            Ok(guard) => guard,
            Err(_) => {
                return "{\"ok\":false,\"error\":\"runtime_state_lock_failed\"}".to_string();
            }
        };

        let mut routes_json = Vec::new();

        for route in self.routes.iter() {
            if route.route_type == "static" {
                continue;
            }

            let mut upstreams_json = Vec::new();

            for upstream in &route.upstreams {
                let key = runtime_state_key(&route.path, &upstream.address);

                let default_state = RuntimeUpstreamState {
                    healthy: true,
                    consecutive_successes: 0,
                    consecutive_failures: 0,
                    last_status: None,
                    last_error: None,
                    last_checked_unix_ms: 0,
                };

                let item = state.get(&key).unwrap_or(&default_state);

                let last_status_json = match item.last_status {
                    Some(status) => status.to_string(),
                    None => "null".to_string(),
                };

                let last_error_json = match &item.last_error {
                    Some(error) => format!("\"{}\"", json_escape(error)),
                    None => "null".to_string(),
                };

                upstreams_json.push(format!(
                    "{{\"address\":\"{}\",\"healthy\":{},\"consecutive_successes\":{},\"consecutive_failures\":{},\"last_status\":{},\"last_error\":{},\"last_checked_unix_ms\":{}}}",
                    json_escape(&upstream.address),
                    item.healthy,
                    item.consecutive_successes,
                    item.consecutive_failures,
                    last_status_json,
                    last_error_json,
                    item.last_checked_unix_ms
                ));
            }

            routes_json.push(format!(
                "{{\"path\":\"{}\",\"balancing\":\"{}\",\"upstreams\":[{}]}}",
                json_escape(&route.path),
                json_escape(&route.balancing),
                upstreams_json.join(",")
            ));
        }

        format!(
            "{{\"ok\":true,\"health_check\":{{\"path\":\"{}\",\"interval_seconds\":{},\"timeout_seconds\":{},\"healthy_threshold\":{},\"unhealthy_threshold\":{}}},\"routes\":[{}]}}",
            json_escape(HEALTH_CHECK_PATH),
            HEALTH_INTERVAL_SECONDS,
            HEALTH_TIMEOUT_SECONDS,
            HEALTHY_THRESHOLD,
            UNHEALTHY_THRESHOLD,
            routes_json.join(",")
        )
    }

    async fn serve_static_route(
        &self,
        session: &mut Session,
        route: &RouteConfig,
        request_path: &str,
    ) -> Result<bool> {
        let relative_path = if route.path == "/" {
            request_path
        } else {
            request_path
                .strip_prefix(route.path.trim_end_matches('/'))
                .unwrap_or("")
        };

        let file_path = match safe_static_path(&route.root, relative_path, &route.index) {
            Some(path) => path,
            None => {
                session.respond_error(403).await?;
                return Ok(true);
            }
        };

        let body = match std::fs::read(&file_path) {
            Ok(value) => value,
            Err(_) => {
                session.respond_error(404).await?;
                return Ok(true);
            }
        };

        let mut header = ResponseHeader::build(200, None)?;
        header.insert_header("content-type", content_type_for_path(&file_path))?;
        header.insert_header("content-length", body.len().to_string())?;

        session.write_response_header(Box::new(header), false).await?;

        if session.req_header().method.as_str().eq_ignore_ascii_case("HEAD") {
            session.write_response_body(None, true).await?;
        } else {
            session
                .write_response_body(Some(Bytes::from(body)), true)
                .await?;
        }

        Ok(true)
    }

    fn upstream_is_healthy(&self, route: &RouteConfig, upstream: &UpstreamConfig) -> bool {
        let key = runtime_state_key(&route.path, &upstream.address);

        let state = match self.runtime_state.lock() {
            Ok(guard) => guard,
            Err(_) => return true,
        };

        state.get(&key).map(|item| item.healthy).unwrap_or(true)
    }

    fn eligible_upstream_indices(&self, route_index: usize) -> Vec<usize> {
        let route = match self.routes.get(route_index) {
            Some(value) => value,
            None => return Vec::new(),
        };

        route
            .upstreams
            .iter()
            .enumerate()
            .filter_map(|(index, upstream)| {
                if self.upstream_is_healthy(route, upstream) {
                    Some(index)
                } else {
                    None
                }
            })
            .collect::<Vec<_>>()
    }

    fn has_healthy_upstream(&self, route_index: usize) -> bool {
        !self.eligible_upstream_indices(route_index).is_empty()
    }

    fn select_upstream(&self, route_index: usize, session: &Session) -> Option<String> {
        let route = self.routes.get(route_index)?;

        if route.upstreams.is_empty() {
            return None;
        }

        let eligible = self.eligible_upstream_indices(route_index);

        if eligible.is_empty() {
            return None;
        }

        let selected_original_index = match route.balancing.as_str() {
            "random" => {
                let pick = self.select_random_index(route_index, eligible.len());
                eligible[pick]
            }
            "weighted_round_robin" => {
                self.select_weighted_round_robin_index(route_index, route, &eligible)
            }
            "least_connections" => self.select_least_connections_index(route_index, &eligible),
            "ip_hash" => {
                let pick = self.select_ip_hash_index(session, eligible.len());
                eligible[pick]
            }
            _ => {
                let pick = self.select_round_robin_index(route_index, eligible.len());
                eligible[pick]
            }
        };

        Some(route.upstreams[selected_original_index].address.clone())
    }

    fn select_round_robin_index(&self, route_index: usize, len: usize) -> usize {
        let counter = self.counters[route_index].fetch_add(1, Ordering::Relaxed);
        counter % len
    }

    fn select_random_index(&self, route_index: usize, len: usize) -> usize {
        let counter = self.counters[route_index].fetch_add(1, Ordering::Relaxed);

        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_else(|_| Duration::from_secs(0))
            .subsec_nanos() as usize;

        let mixed = counter
            .wrapping_mul(1_103_515_245usize)
            .wrapping_add(12_345usize)
            .wrapping_add(nanos)
            .wrapping_add(route_index.wrapping_mul(2_654_435_761usize));

        mixed % len
    }

    fn select_weighted_round_robin_index(
        &self,
        route_index: usize,
        route: &RouteConfig,
        eligible: &[usize],
    ) -> usize {
        let total_weight: usize = eligible
            .iter()
            .map(|index| route.upstreams[*index].weight.max(1))
            .sum();

        if total_weight == 0 {
            let pick = self.select_round_robin_index(route_index, eligible.len());
            return eligible[pick];
        }

        let counter = self.counters[route_index].fetch_add(1, Ordering::Relaxed);
        let mut ticket = counter % total_weight;

        for index in eligible {
            let weight = route.upstreams[*index].weight.max(1);

            if ticket < weight {
                return *index;
            }

            ticket -= weight;
        }

        eligible[0]
    }

    fn select_least_connections_index(&self, route_index: usize, eligible: &[usize]) -> usize {
        let loads = match self.upstream_loads.get(route_index) {
            Some(value) => value,
            None => return eligible[0],
        };

        let mut best_index = eligible[0];
        let mut best_load = usize::MAX;

        for index in eligible {
            let load = loads
                .get(*index)
                .map(|counter| counter.load(Ordering::Relaxed))
                .unwrap_or(usize::MAX);

            if load < best_load {
                best_load = load;
                best_index = *index;
            }
        }

        if let Some(counter) = loads.get(best_index) {
            counter.fetch_add(1, Ordering::Relaxed);
        }

        best_index
    }

    fn select_ip_hash_index(&self, session: &Session, len: usize) -> usize {
        let key = client_key(session);
        let mut hash: usize = 2_166_136_261usize;

        for byte in key.as_bytes() {
            hash ^= *byte as usize;
            hash = hash.wrapping_mul(16_777_619usize);
        }

        hash % len
    }

    fn is_blocked_path(&self, path: &str) -> bool {
        for blocked in &self.security.blocked_paths {
            if path == blocked || path.starts_with(&format!("{}/", blocked.trim_end_matches('/'))) {
                return true;
            }
        }

        false
    }

    fn method_allowed(&self, method: &str) -> bool {
        if self.security.allowed_methods.is_empty() {
            return true;
        }

        self.security
            .allowed_methods
            .iter()
            .any(|allowed| allowed.eq_ignore_ascii_case(method))
    }

    fn check_rate_limit(&self, client_key: &str) -> bool {
        let limit = self.security.rate_limit_per_minute;

        if limit == 0 {
            return true;
        }

        let mut limiter = match self.rate_limiter.lock() {
            Ok(guard) => guard,
            Err(_) => return true,
        };

        let now = Instant::now();

        let bucket = limiter.entry(client_key.to_string()).or_insert(RateBucket {
            window_start: now,
            count: 0,
        });

        if now.duration_since(bucket.window_start) >= Duration::from_secs(60) {
            bucket.window_start = now;
            bucket.count = 0;
        }

        if bucket.count >= limit {
            return false;
        }

        bucket.count += 1;
        true
    }
}

fn parse_upstream(upstream: &str) -> (String, u16) {
    let cleaned = upstream
        .trim()
        .trim_start_matches("http://")
        .trim_start_matches("https://")
        .trim_end_matches('/');

    let mut parts = cleaned.rsplitn(2, ':');

    let port_str = parts.next().unwrap_or("80");
    let host = parts.next().unwrap_or(cleaned);

    let port = port_str.parse::<u16>().unwrap_or(80);

    (host.to_string(), port)
}

fn check_upstream_health(
    upstream: &str,
    path: &str,
    timeout_seconds: u64,
) -> (bool, Option<u16>, Option<String>) {
    let (host, port) = parse_upstream(upstream);
    let health_path = if path.starts_with('/') {
        path.to_string()
    } else {
        format!("/{}", path)
    };

    let timeout = Duration::from_secs(timeout_seconds);

    let addresses = match (host.as_str(), port).to_socket_addrs() {
        Ok(value) => value.collect::<Vec<_>>(),
        Err(error) => {
            return (
                false,
                None,
                Some(format!("resolve_failed: {}", error)),
            );
        }
    };

    if addresses.is_empty() {
        return (
            false,
            None,
            Some("resolve_failed: no_socket_addresses".to_string()),
        );
    }

    let mut last_error = None;

    for address in addresses {
        match TcpStream::connect_timeout(&address, timeout) {
            Ok(mut stream) => {
                let _ = stream.set_read_timeout(Some(timeout));
                let _ = stream.set_write_timeout(Some(timeout));

                let request = format!(
                    "GET {} HTTP/1.1\r\nHost: {}:{}\r\nConnection: close\r\nUser-Agent: pingora-ai-health/0.1\r\n\r\n",
                    health_path, host, port
                );

                if let Err(error) = stream.write_all(request.as_bytes()) {
                    last_error = Some(format!("write_failed: {}", error));
                    continue;
                }

                let mut buffer = [0u8; 512];

                match stream.read(&mut buffer) {
                    Ok(size) if size > 0 => {
                        let response = String::from_utf8_lossy(&buffer[..size]);
                        let status = response
                            .lines()
                            .next()
                            .and_then(|line| line.split_whitespace().nth(1))
                            .and_then(|value| value.parse::<u16>().ok());

                        match status {
                            Some(code) => {
                                return (code < 500, Some(code), None);
                            }
                            None => {
                                return (
                                    false,
                                    None,
                                    Some("invalid_http_response".to_string()),
                                );
                            }
                        }
                    }
                    Ok(_) => {
                        last_error = Some("empty_response".to_string());
                    }
                    Err(error) => {
                        last_error = Some(format!("read_failed: {}", error));
                    }
                }
            }
            Err(error) => {
                last_error = Some(format!("connect_failed: {}", error));
            }
        }
    }

    (false, None, last_error)
}

fn client_key(session: &Session) -> String {
    match session.client_addr() {
        Some(addr) => {
            let raw = format!("{:?}", addr);

            if raw.starts_with('[') {
                match raw.rfind("]:") {
                    Some(index) => raw[..index + 1].to_string(),
                    None => raw,
                }
            } else {
                match raw.rfind(':') {
                    Some(index) => raw[..index].to_string(),
                    None => raw,
                }
            }
        }
        None => "unknown".to_string(),
    }
}

#[async_trait]
impl ProxyHttp for GeneratedProxy {
    type CTX = RequestContext;

    fn new_ctx(&self) -> Self::CTX {
        RequestContext {}
    }

    async fn request_filter(
        &self,
        session: &mut Session,
        _ctx: &mut Self::CTX,
    ) -> Result<bool> {
        let path = session.req_header().uri.path().to_string();
        let method = session.req_header().method.as_str().to_uppercase();

        if path == "/__gateway_runtime_state" {
            if method != "GET" && method != "HEAD" {
                session.respond_error(405).await?;
                return Ok(true);
            }

            return self.serve_runtime_state(session).await;
        }

        if !self.method_allowed(&method) {
            session.respond_error(405).await?;
            return Ok(true);
        }

        if self.is_blocked_path(&path) {
            session.respond_error(403).await?;
            return Ok(true);
        }

        if session.req_header().headers.get("upgrade").is_some() {
            session.respond_error(400).await?;
            return Ok(true);
        }

        if let Some(value) = session.req_header().headers.get("content-length") {
            if let Ok(text) = value.to_str() {
                if let Ok(length) = text.parse::<usize>() {
                    if length > self.security.max_request_body_bytes {
                        session.respond_error(413).await?;
                        return Ok(true);
                    }
                }
            }
        }

        let key = client_key(session);

        if !self.check_rate_limit(&key) {
            session.respond_error(429).await?;
            return Ok(true);
        }

        let route_index = match self.match_route_index(&path) {
            Some(index) => index,
            None => {
                session.respond_error(404).await?;
                return Ok(true);
            }
        };

        let route = match self.routes.get(route_index) {
            Some(value) => value.clone(),
            None => {
                session.respond_error(404).await?;
                return Ok(true);
            }
        };

        if route.route_type == "static" {
            return self.serve_static_route(session, &route, &path).await;
        }

        if !self.has_healthy_upstream(route_index) {
            session.respond_error(503).await?;
            return Ok(true);
        }

        Ok(false)
    }

    async fn upstream_peer(
        &self,
        session: &mut Session,
        _ctx: &mut Self::CTX,
    ) -> Result<Box<HttpPeer>> {
        let path = session.req_header().uri.path();

        let route_index = self.match_route_index(path).unwrap_or(0);

        let upstream = self
            .select_upstream(route_index, session)
            .or_else(|| {
                self.routes
                    .get(route_index)
                    .and_then(|route| route.upstreams.first())
                    .map(|upstream| upstream.address.clone())
            })
            .unwrap_or_else(|| "127.0.0.1:3000".to_string());

        let (host, port) = parse_upstream(upstream.as_str());

        let peer = Box::new(HttpPeer::new(
            (host.as_str(), port),
            false,
            String::new(),
        ));

        Ok(peer)
    }
}

fn build_routes() -> Vec<RouteConfig> {
    vec![
    RouteConfig {
        path: "/".to_string(),
        route_type: "static".to_string(),
        root: "public".to_string(),
        index: "index.html".to_string(),
        balancing: "single".to_string(),
        upstreams: vec![
        ],
    },
    RouteConfig {
        path: "/api".to_string(),
        route_type: "proxy".to_string(),
        root: "public".to_string(),
        index: "index.html".to_string(),
        balancing: "single".to_string(),
        upstreams: vec![
        UpstreamConfig {
            address: "127.0.0.1:9101".to_string(),
            weight: 1,
        },
        ],
    },
    ]
}

fn build_security() -> SecurityConfig {
    SecurityConfig {
        blocked_paths: vec!["/.env".to_string(), "/.git".to_string(), "/admin.php".to_string(), "/phpmyadmin".to_string(), "/wp-admin".to_string(), "/wp-login.php".to_string()],
        allowed_methods: vec!["GET".to_string(), "POST".to_string(), "PUT".to_string(), "PATCH".to_string(), "DELETE".to_string(), "OPTIONS".to_string(), "HEAD".to_string()],
        rate_limit_per_minute: 10000,
        max_connections: 1000,
        max_request_body_bytes: 1048576,
        upstream_timeout_seconds: 30,
    }
}

fn resolve_listen_port() -> u16 {
    let args = env::args().collect::<Vec<_>>();

    for index in 0..args.len() {
        if args[index] == "--port" {
            if let Some(value) = args.get(index + 1) {
                if let Ok(port) = value.parse::<u16>() {
                    return port;
                }
            }
        }

        if let Some(value) = args[index].strip_prefix("--port=") {
            if let Ok(port) = value.parse::<u16>() {
                return port;
            }
        }
    }

    if let Ok(value) = env::var("PINGORA_AI_LISTEN_PORT") {
        if let Ok(port) = value.parse::<u16>() {
            return port;
        }
    }

    GATEWAY_LISTEN_PORT
}

fn main() {
    let routes = build_routes();
    let security = build_security();

    let proxy = GeneratedProxy::new(routes, security);

    let mut server = Server::new(None).unwrap();
    server.bootstrap();

    let listen_port = resolve_listen_port();
    let listen_addr = format!("0.0.0.0:{}", listen_port);

    let mut service = http_proxy_service(&server.configuration, proxy);
    service.add_tcp(&listen_addr);

    server.add_service(service);

    println!("🚀 Starting Pingora proxy on port {}", listen_port);
    println!("🩺 Runtime upstream health checks enabled");
    println!("📊 Runtime state endpoint: /__gateway_runtime_state");

    server.run_forever();
}
