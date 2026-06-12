from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates" / "pingora_runtime" / "src" / "main.rs"
TARGET = ROOT / "generated-pingora-proxy" / "src" / "main.rs"


def fail(message: str) -> None:
    raise SystemExit(f"❌ {message}")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if old not in text:
        fail(f"Could not find {label}.")
    return text.replace(old, new, 1)


def insert_before_once(text: str, marker: str, insert: str, label: str) -> str:
    if insert.strip() in text:
        return text
    if marker not in text:
        fail(f"Could not find marker for {label}.")
    return text.replace(marker, insert + marker, 1)


def main() -> int:
    if not TARGET.exists():
        fail(f"Missing target runtime: {TARGET}")

    text = TARGET.read_text(encoding="utf-8")

    template_integrated_markers = [
        "struct CachedResponse",
        "struct InFlightCacheCapture",
        "cache_capture: Option<InFlightCacheCapture>",
        "response_cache: Arc<Mutex<HashMap<String, CachedResponse>>>",
        "cache_hits: Arc<AtomicUsize>",
        "cache_misses: Arc<AtomicUsize>",
        "fn route_cache_enabled",
        "fn cache_key_for_request",
        "fn cached_response_for_key",
        "async fn response_filter",
        "x-pingora-ai-cache",
        "__gateway_runtime_admin/cache/clear",
        "created_unix_ms",
        "MAX_CACHE_ENTRIES",
        "\"policy\":{\"max_entries\":1024",
    ]

    if all(marker in text for marker in template_integrated_markers):
        print("✅ Manual cache already template-integrated; compatibility patch skipped.")
        return 0

    if (
        "struct CachedResponse" in text
        and "response_cache: Arc<Mutex<HashMap<String, CachedResponse>>>" in text
        and "fn response_body_filter(" in text
        and "x-pingora-ai-cache" in text
    ):
        if "created_unix_ms: u128" not in text:
            text = text.replace(
                """struct CachedResponse {
    header: ResponseHeader,
    body: Vec<u8>,
    expires_unix_ms: u128,
}
""",
                """struct CachedResponse {
    header: ResponseHeader,
    body: Vec<u8>,
    expires_unix_ms: u128,
    created_unix_ms: u128,
}
""",
                1,
            )

        if "__gateway_runtime_admin/cache/clear" not in text:
            text = text.replace(
                """        if path == "/__gateway_runtime_state" {
""",
                """        if path == "/__gateway_runtime_admin/cache/clear" {
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
                "{{\\"ok\\":true,\\"action\\":\\"clear_cache\\",\\"scope\\":\\"all\\",\\"cleared_entries\\":{},\\"local_only\\":true}}",
                cleared
            );

            return self.write_json_response(session, 200, body).await;
        }

        if path == "/__gateway_runtime_state" {
""",
                1,
            )

        if "MAX_CACHE_ENTRIES" not in text:
            text = text.replace(
                """        const MAX_CACHE_BODY_BYTES: usize = 1024 * 1024;
""",
                """        const MAX_CACHE_BODY_BYTES: usize = 1024 * 1024;
        const MAX_CACHE_ENTRIES: usize = 1024;
""",
                1,
            )


        if "\"policy\":{\"max_entries\":1024" not in text:
            old_policy_json = '        let cache_entries = self\n            .response_cache\n            .lock()\n            .map(|cache| cache.len())\n            .unwrap_or(0);\n\n        format!(\n            "{{\\"ok\\":true,\\"health_check\\":{{\\"path\\":\\"{}\\",\\"interval_seconds\\":{},\\"timeout_seconds\\":{},\\"healthy_threshold\\":{},\\"unhealthy_threshold\\":{}}},\\"routes\\":[{}],\\"cache\\":{{\\"eligible\\":{},\\"bypasses\\":{},\\"hits\\":{},\\"misses\\":{},\\"entries\\":{}}}}}",\n            json_escape(HEALTH_CHECK_PATH),\n            HEALTH_INTERVAL_SECONDS,\n            HEALTH_TIMEOUT_SECONDS,\n            HEALTHY_THRESHOLD,\n            UNHEALTHY_THRESHOLD,\n            routes_json.join(","),\n            self.cache_eligible.load(Ordering::Relaxed),\n            self.cache_bypasses.load(Ordering::Relaxed),\n            self.cache_hits.load(Ordering::Relaxed),\n            self.cache_misses.load(Ordering::Relaxed),\n            cache_entries\n        )\n'
            new_policy_json = '        let cache_entries = self\n            .response_cache\n            .lock()\n            .map(|cache| cache.len())\n            .unwrap_or(0);\n\n        let mut cache_policy_routes = Vec::new();\n\n        for route in self.routes.iter() {\n            if let Some(cache) = &route.cache {\n                if cache.enabled {\n                    let methods_json = cache\n                        .methods\n                        .iter()\n                        .map(|method| format!("\\"{}.\\"", json_escape(method)).replace(".\\"", "\\""))\n                        .collect::<Vec<_>>()\n                        .join(",");\n\n                    let bypass_headers_json = cache\n                        .bypass_headers\n                        .iter()\n                        .map(|header| format!("\\"{}.\\"", json_escape(header)).replace(".\\"", "\\""))\n                        .collect::<Vec<_>>()\n                        .join(",");\n\n                    cache_policy_routes.push(format!(\n                        "{{\\"path\\":\\"{}\\",\\"ttl_seconds\\":{},\\"methods\\":[{}],\\"cache_key\\":\\"{}\\",\\"bypass_headers\\":[{}]}}",\n                        json_escape(&route.path),\n                        cache.ttl_seconds.clamp(1, 86400),\n                        methods_json,\n                        json_escape(&cache.cache_key),\n                        bypass_headers_json\n                    ));\n                }\n            }\n        }\n\n        format!(\n            "{{\\"ok\\":true,\\"health_check\\":{{\\"path\\":\\"{}\\",\\"interval_seconds\\":{},\\"timeout_seconds\\":{},\\"healthy_threshold\\":{},\\"unhealthy_threshold\\":{}}},\\"routes\\":[{}],\\"cache\\":{{\\"eligible\\":{},\\"bypasses\\":{},\\"hits\\":{},\\"misses\\":{},\\"entries\\":{},\\"policy\\":{{\\"max_entries\\":1024,\\"max_body_bytes\\":1048576,\\"enabled_routes\\":[{}]}}}}}}",\n            json_escape(HEALTH_CHECK_PATH),\n            HEALTH_INTERVAL_SECONDS,\n            HEALTH_TIMEOUT_SECONDS,\n            HEALTHY_THRESHOLD,\n            UNHEALTHY_THRESHOLD,\n            routes_json.join(","),\n            self.cache_eligible.load(Ordering::Relaxed),\n            self.cache_bypasses.load(Ordering::Relaxed),\n            self.cache_hits.load(Ordering::Relaxed),\n            self.cache_misses.load(Ordering::Relaxed),\n            cache_entries,\n            cache_policy_routes.join(",")\n        )\n'
            if old_policy_json in text:
                text = text.replace(old_policy_json, new_policy_json, 1)
            elif "max_entries" in text and "max_body_bytes" in text and "enabled_routes" in text:
                print("✅ runtime state cache policy observability already template-managed.")
            else:
                fail("Could not find runtime state cache JSON block for policy observability.")

        if "created_unix_ms: current_unix_ms()" not in text:
            text = text.replace(
                """                        if let Ok(mut cache) = self.response_cache.lock() {
                            cache.insert(
                                capture.key.clone(),
                                CachedResponse {
                                    header,
                                    body: capture.body.clone(),
                                    expires_unix_ms,
                                },
                            );
                        }
""",
                """                        if let Ok(mut cache) = self.response_cache.lock() {
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
""",
                1,
            )

        TARGET.write_text(text, encoding="utf-8")
        print("✅ Manual cache invalidation added to existing Step 6 generated Rust.")
        return 0

    # 1. Add cache response/capture structs and richer RequestContext.
    old = '''#[derive(Debug)]
struct RequestContext {}
'''
    new = '''#[derive(Clone, Debug)]
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
'''
    text = replace_once(text, old, new, "RequestContext block")

    # 2. Add GeneratedProxy cache fields.
    old = '''    runtime_state: Arc<Mutex<HashMap<String, RuntimeUpstreamState>>>,
    admin_upstream_state: Arc<Mutex<HashMap<String, RuntimeAdminUpstreamState>>>,
    cache_eligible: Arc<AtomicUsize>,
    cache_bypasses: Arc<AtomicUsize>,
}
'''
    new = '''    runtime_state: Arc<Mutex<HashMap<String, RuntimeUpstreamState>>>,
    admin_upstream_state: Arc<Mutex<HashMap<String, RuntimeAdminUpstreamState>>>,
    cache_eligible: Arc<AtomicUsize>,
    cache_bypasses: Arc<AtomicUsize>,
    cache_hits: Arc<AtomicUsize>,
    cache_misses: Arc<AtomicUsize>,
    response_cache: Arc<Mutex<HashMap<String, CachedResponse>>>,
}
'''
    text = replace_once(text, old, new, "GeneratedProxy cache fields")

    # 3. Add constructor fields.
    old = '''            runtime_state,
            admin_upstream_state,
            cache_eligible: Arc::new(AtomicUsize::new(0)),
            cache_bypasses: Arc::new(AtomicUsize::new(0)),
        };
'''
    new = '''            runtime_state,
            admin_upstream_state,
            cache_eligible: Arc::new(AtomicUsize::new(0)),
            cache_bypasses: Arc::new(AtomicUsize::new(0)),
            cache_hits: Arc::new(AtomicUsize::new(0)),
            cache_misses: Arc::new(AtomicUsize::new(0)),
            response_cache: Arc::new(Mutex::new(HashMap::new())),
        };
'''
    text = replace_once(text, old, new, "constructor cache fields")

    # 4. Add cache helpers before is_blocked_path.
    marker = '''    fn is_blocked_path(&self, path: &str) -> bool {
'''
    insert = '''    fn route_cache_allows_method(route: &RouteConfig, method: &str) -> bool {
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

'''
    text = insert_before_once(text, marker, insert, "manual cache helpers")

    # 5. Runtime JSON cache counters.
    old = '''        format!(
            "{{\\"ok\\":true,\\"health_check\\":{{\\"path\\":\\"{}\\",\\"interval_seconds\\":{},\\"timeout_seconds\\":{},\\"healthy_threshold\\":{},\\"unhealthy_threshold\\":{}}},\\"routes\\":[{}],\\"cache\\":{{\\"eligible\\":{},\\"bypasses\\":{}}}}}",
            json_escape(HEALTH_CHECK_PATH),
            HEALTH_INTERVAL_SECONDS,
            HEALTH_TIMEOUT_SECONDS,
            HEALTHY_THRESHOLD,
            UNHEALTHY_THRESHOLD,
            routes_json.join(","),
            self.cache_eligible.load(Ordering::Relaxed),
            self.cache_bypasses.load(Ordering::Relaxed)
        )
'''
    new = '''        let cache_entries = self
            .response_cache
            .lock()
            .map(|cache| cache.len())
            .unwrap_or(0);

        format!(
            "{{\\"ok\\":true,\\"health_check\\":{{\\"path\\":\\"{}\\",\\"interval_seconds\\":{},\\"timeout_seconds\\":{},\\"healthy_threshold\\":{},\\"unhealthy_threshold\\":{}}},\\"routes\\":[{}],\\"cache\\":{{\\"eligible\\":{},\\"bypasses\\":{},\\"hits\\":{},\\"misses\\":{},\\"entries\\":{}}}}}",
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
            cache_entries
        )
'''
    text = replace_once(text, old, new, "runtime state cache JSON block")

    # 6. new_ctx.
    old = '''    fn new_ctx(&self) -> Self::CTX {
        RequestContext {}
    }
'''
    new = '''    fn new_ctx(&self) -> Self::CTX {
        RequestContext {
            cache_route_index: None,
            cache_key: None,
            cache_capture: None,
        }
    }
'''
    text = replace_once(text, old, new, "new_ctx block")

    # 7. Request filter HIT/MISS setup.
    old = '''        self.record_cache_gate(session, &route);

        if route.route_type == "static" {
'''
    new = '''        self.record_cache_gate(session, &route);

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
'''
    text = replace_once(text, old, new, "request_filter cache setup")

    # 8. response_filter + response_body_filter before upstream_peer.
    marker = '''    async fn upstream_peer(
'''
    insert = '''    async fn response_filter(
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

'''
    text = insert_before_once(text, marker, insert, "manual cache response hooks")

    TARGET.write_text(text, encoding="utf-8")
    print("✅ Manual cache Step 6 patched into generated Rust.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
