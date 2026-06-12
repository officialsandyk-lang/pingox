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
    state: String,
    enabled: bool,
    draining: bool,
    role: String,
    backup: bool,
}

#[derive(Clone, Debug)]
struct CacheConfig {
    enabled: bool,
    ttl_seconds: u64,
    methods: Vec<String>,
    cache_key: String,
    bypass_headers: Vec<String>,
    stale_while_revalidate_seconds: u64,
}

#[derive(Clone, Debug)]
struct RouteConfig {
    path: String,
    route_type: String,
    root: String,
    index: String,
    balancing: String,
    cache: Option<CacheConfig>,
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

#[derive(Clone, Debug)]
struct CachedResponse {
    header: ResponseHeader,
    body: Vec<u8>,
    expires_unix_ms: u128,
    created_unix_ms: u128,
}

#[derive(Debug)]
struct InFlightCacheCapture {
    key: String,
    header: Option<ResponseHeader>,
    body: Vec<u8>,
    ttl_seconds: u64,
    cacheable: bool,
}

#[derive(Debug)]
struct RequestContext {
    cache_route_index: Option<usize>,
    cache_key: Option<String>,
    cache_capture: Option<InFlightCacheCapture>,
}

#[derive(Clone)]
struct GeneratedProxy {
    routes: Arc<Vec<RouteConfig>>,
    counters: Arc<Vec<AtomicUsize>>,
    upstream_loads: Arc<Vec<Vec<AtomicUsize>>>,
    security: SecurityConfig,
    rate_limiter: Arc<Mutex<HashMap<String, RateBucket>>>,
    runtime_state: Arc<Mutex<HashMap<String, RuntimeUpstreamState>>>,
    cache_eligible: Arc<AtomicUsize>,
    cache_bypasses: Arc<AtomicUsize>,
    cache_hits: Arc<AtomicUsize>,
    cache_misses: Arc<AtomicUsize>,
    response_cache: Arc<Mutex<HashMap<String, CachedResponse>>>,
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

fn default_cache_config() -> CacheConfig {
    CacheConfig {
        enabled: false,
        ttl_seconds: 30,
        methods: vec!["GET".to_string()],
        cache_key: "method:path:query".to_string(),
        bypass_headers: vec![
            "authorization".to_string(),
            "x-api-key".to_string(),
        ],
        stale_while_revalidate_seconds: 0,
    }
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
            cache_eligible: Arc::new(AtomicUsize::new(0)),
            cache_bypasses: Arc::new(AtomicUsize::new(0)),
            cache_hits: Arc::new(AtomicUsize::new(0)),
            cache_misses: Arc::new(AtomicUsize::new(0)),
            response_cache: Arc::new(Mutex::new(HashMap::new())),
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

        let cache_entries = self
            .response_cache
            .lock()
            .map(|cache| cache.len())
            .unwrap_or(0);

        let mut cache_policy_routes = Vec::new();

        for route in self.routes.iter() {
            if let Some(cache) = &route.cache {
                if cache.enabled {
                    let methods_json = cache
                        .methods
                        .iter()
                        .map(|method| format!("\"{}.\"", json_escape(method)).replace(".\"", "\""))
                        .collect::<Vec<_>>()
                        .join(",");

                    let bypass_headers_json = cache
                        .bypass_headers
                        .iter()
                        .map(|header| format!("\"{}.\"", json_escape(header)).replace(".\"", "\""))
                        .collect::<Vec<_>>()
                        .join(",");

                    cache_policy_routes.push(format!(
                        "{{\"path\":\"{}\",\"ttl_seconds\":{},\"methods\":[{}],\"cache_key\":\"{}\",\"bypass_headers\":[{}]}}",
                        json_escape(&route.path),
                        cache.ttl_seconds.clamp(1, 86400),
                        methods_json,
                        json_escape(&cache.cache_key),
                        bypass_headers_json
                    ));
                }
            }
        }

        format!(
            "{{\"ok\":true,\"health_check\":{{\"path\":\"{}\",\"interval_seconds\":{},\"timeout_seconds\":{},\"healthy_threshold\":{},\"unhealthy_threshold\":{}}},\"routes\":[{}],\"cache\":{{\"eligible\":{},\"bypasses\":{},\"hits\":{},\"misses\":{},\"entries\":{},\"policy\":{{\"max_entries\":1024,\"max_body_bytes\":1048576,\"enabled_routes\":[{}]}}}}}}",
            json_escape(HEALTH_CHECK_PATH),
            HEALTH_INTERVAL_SECONDS,
            HEALTH_TIMEOUT_SECONDS,
            HEALTHY_THRESHOLD,
            UNHEALTHY_THRESHOLD,
            routes_json.join(","),
            self.cache_eligible.load(Ordering::Relaxed),
            self.cache_bypasses.load(Ordering::Relaxed),
            self.cache_hits.load(Ordering::Relaxed),
            self.cache_misses.load(Ordering::Relaxed),
            cache_entries,
            cache_policy_routes.join(",")
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

    fn route_cache_enabled(route: &RouteConfig) -> bool {
        route
            .cache
            .as_ref()
            .map(|cache| cache.enabled)
            .unwrap_or(false)
    }

    fn request_has_cache_bypass_headers(session: &Session, route: &RouteConfig) -> bool {
        let mut bypass_headers = vec![
            "authorization".to_string(),
            "x-api-key".to_string(),
        ];

        if let Some(cache) = &route.cache {
            for header in &cache.bypass_headers {
                let normalized = header.to_lowercase();
                if !bypass_headers.iter().any(|value| value == &normalized) {
                    bypass_headers.push(normalized);
                }
            }
        }

        for header in bypass_headers {
            if session.req_header().headers.get(header.as_str()).is_some() {
                return true;
            }
        }

        false
    }

    fn record_cache_gate(&self, session: &Session, route: &RouteConfig) {
        let method = session.req_header().method.as_str().to_uppercase();

        if method != "GET" && method != "HEAD" {
            return;
        }

        if !Self::route_cache_enabled(route) {
            return;
        }

        if Self::request_has_cache_bypass_headers(session, route) {
            self.cache_bypasses.fetch_add(1, Ordering::Relaxed);
            return;
        }

        self.cache_eligible.fetch_add(1, Ordering::Relaxed);
    }

    fn route_cache_allows_method(route: &RouteConfig, method: &str) -> bool {
        route
            .cache
            .as_ref()
            .map(|cache| cache.methods.iter().any(|item| item.eq_ignore_ascii_case(method)))
            .unwrap_or(false)
    }

    fn route_cache_ttl_seconds(route: &RouteConfig) -> u64 {
        route
            .cache
            .as_ref()
            .map(|cache| cache.ttl_seconds)
            .unwrap_or(30)
            .clamp(1, 86400)
    }

    fn cache_key_for_request(&self, session: &Session, route: &RouteConfig) -> String {
        let method = session.req_header().method.as_str().to_uppercase();
        let uri = session.req_header().uri.clone();
        let path = uri.path().to_string();
        let query = uri.query().unwrap_or("").to_string();

        let key_mode = route
            .cache
            .as_ref()
            .map(|cache| cache.cache_key.as_str())
            .unwrap_or("method:path:query");

        if key_mode == "method:path" {
            format!("{}:{}", method, path)
        } else {
            format!("{}:{}?{}", method, path, query)
        }
    }

    fn cached_response_for_key(&self, key: &str) -> Option<CachedResponse> {
        let now = current_unix_ms();

        let mut cache = self.response_cache.lock().ok()?;
        match cache.get(key) {
            Some(entry) if entry.expires_unix_ms > now => Some(entry.clone()),
            Some(_) => {
                cache.remove(key);
                None
            }
            None => None,
        }
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
        RequestContext {
            cache_route_index: None,
            cache_key: None,
            cache_capture: None,
        }
    }

    async fn request_filter(
        &self,
        session: &mut Session,
        _ctx: &mut Self::CTX,
    ) -> Result<bool> {
        let path = session.req_header().uri.path().to_string();
        let method = session.req_header().method.as_str().to_uppercase();

        if path == "/__gateway_runtime_admin/cache/clear" {
            if method != "POST" {
                session.respond_error(405).await?;
                return Ok(true);
            }

            let cleared = match self.response_cache.lock() {
                Ok(mut cache) => {
                    let count = cache.len();
                    cache.clear();
                    count
                }
                Err(_) => 0,
            };

            let body = format!(
                "{{\"ok\":true,\"action\":\"clear_cache\",\"scope\":\"all\",\"cleared_entries\":{},\"local_only\":true}}",
                cleared
            );

            return self.write_json_response(session, 200, body).await;
        }

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

        self.record_cache_gate(session, &route);

        let cache_allowed = Self::route_cache_enabled(&route)
            && Self::route_cache_allows_method(&route, &method)
            && !Self::request_has_cache_bypass_headers(session, &route)
            && route.route_type != "static";

        if cache_allowed {
            let cache_key = self.cache_key_for_request(session, &route);

            if let Some(cached) = self.cached_response_for_key(&cache_key) {
                let mut header = cached.header.clone();
                let _ = header.insert_header("x-pingora-ai-cache", "HIT");
                let end_stream = method == "HEAD" || cached.body.is_empty();

                session.write_response_header(Box::new(header), end_stream).await?;

                if method != "HEAD" && !cached.body.is_empty() {
                    session
                        .write_response_body(Some(Bytes::copy_from_slice(&cached.body)), true)
                        .await?;
                } else if end_stream {
                    session.write_response_body(None, true).await?;
                }

                self.cache_hits.fetch_add(1, Ordering::Relaxed);
                return Ok(true);
            }

            self.cache_misses.fetch_add(1, Ordering::Relaxed);
            _ctx.cache_route_index = Some(route_index);
            _ctx.cache_key = Some(cache_key.clone());
            _ctx.cache_capture = Some(InFlightCacheCapture {
                key: cache_key,
                header: None,
                body: Vec::new(),
                ttl_seconds: Self::route_cache_ttl_seconds(&route),
                cacheable: true,
            });
        }

        if route.route_type == "static" {
            return self.serve_static_route(session, &route, &path).await;
        }

        if !self.has_healthy_upstream(route_index) {
            session.respond_error(503).await?;
            return Ok(true);
        }

        Ok(false)
    }

    async fn response_filter(
        &self,
        _session: &mut Session,
        upstream_response: &mut ResponseHeader,
        ctx: &mut Self::CTX,
    ) -> Result<()>
    where
        Self::CTX: Send + Sync,
    {
        if let Some(capture) = &mut ctx.cache_capture {
            if upstream_response.status.as_u16() == 200 {
                let _ = upstream_response.insert_header("x-pingora-ai-cache", "MISS");
                capture.header = Some(upstream_response.clone());
            } else {
                capture.cacheable = false;
            }
        }

        Ok(())
    }

    fn response_body_filter(
        &self,
        _session: &mut Session,
        body: &mut Option<Bytes>,
        end_of_stream: bool,
        ctx: &mut Self::CTX,
    ) -> Result<Option<Duration>>
    where
        Self::CTX: Send + Sync,
    {
        const MAX_CACHE_BODY_BYTES: usize = 1024 * 1024;
        const MAX_CACHE_ENTRIES: usize = 1024;

        if let Some(capture) = &mut ctx.cache_capture {
            if capture.cacheable {
                if let Some(chunk) = body.as_ref() {
                    if capture.body.len().saturating_add(chunk.len()) <= MAX_CACHE_BODY_BYTES {
                        capture.body.extend_from_slice(chunk);
                    } else {
                        capture.cacheable = false;
                    }
                }

                if end_of_stream && capture.cacheable {
                    if let Some(header) = capture.header.clone() {
                        let expires_unix_ms = current_unix_ms()
                            .saturating_add((capture.ttl_seconds as u128) * 1000);

                        if let Ok(mut cache) = self.response_cache.lock() {
                            if cache.len() >= MAX_CACHE_ENTRIES {
                                if let Some(oldest_key) = cache
                                    .iter()
                                    .min_by_key(|(_key, entry)| entry.created_unix_ms)
                                    .map(|(key, _entry)| key.clone())
                                {
                                    cache.remove(&oldest_key);
                                }
                            }

                            cache.insert(
                                capture.key.clone(),
                                CachedResponse {
                                    header,
                                    body: capture.body.clone(),
                                    expires_unix_ms,
                                    created_unix_ms: current_unix_ms(),
                                },
                            );
                        }
                    }
                }
            }
        }

        Ok(None)
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
        cache: Some(default_cache_config()),
        upstreams: vec![
        ],
    },
    RouteConfig {
        path: "/api".to_string(),
        route_type: "proxy".to_string(),
        root: "public".to_string(),
        index: "index.html".to_string(),
        balancing: "single".to_string(),
        cache: Some(default_cache_config()),
        upstreams: vec![
        UpstreamConfig {
            address: "127.0.0.1:9101".to_string(),
            weight: 1,
            state: "enabled".to_string(),
            enabled: true,
            draining: false,
            role: "primary".to_string(),
            backup: false,
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
