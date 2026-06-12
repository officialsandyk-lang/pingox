from pathlib import Path
import re

MAIN_RS = Path("generated-pingora-proxy/src/main.rs")

def fail(msg: str) -> None:
    raise SystemExit(f"❌ {msg}")

def backup(path: Path) -> None:
    b = path.with_suffix(".rs.bak-before-runtime-admin-agent-phase1")
    if not b.exists():
        b.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"✅ Backup created: {b}")

def main() -> int:
    text = MAIN_RS.read_text(encoding="utf-8")

    template_integrated_markers = [
        "struct RuntimeAdminUpstreamState",
        "admin_upstream_state: Arc<Mutex<HashMap<String, RuntimeAdminUpstreamState>>>",
        "async fn serve_runtime_admin_upstream",
        "fn update_admin_upstream_state",
        "fn eligible_upstream_indices_by_role",
        "effective_state",
        "live_admin_override",
    ]

    if all(marker in text for marker in template_integrated_markers):
        print("✅ Runtime admin already template-integrated; compatibility patch skipped.")
        return 0

    backup(MAIN_RS)

    if "PINGORA_AI_RUNTIME_ADMIN_PHASE1" in text:
        print("Runtime admin phase1 already patched.")
        return 0

    # 1. Add admin state struct after RuntimeUpstreamState.
    old = '''struct RuntimeUpstreamState {
    healthy: bool,
    consecutive_successes: usize,
    consecutive_failures: usize,
    last_status: Option<u16>,
    last_error: Option<String>,
    last_checked_unix_ms: u128,
}
'''

    new = old + '''
// PINGORA_AI_RUNTIME_ADMIN_PHASE1
#[derive(Clone, Debug)]
struct RuntimeAdminUpstreamState {
    state: String,
    enabled: bool,
    draining: bool,
    role: String,
    backup: bool,
    updated_unix_ms: u128,
}
'''

    if old not in text:
        fail("Could not find RuntimeUpstreamState struct.")
    text = text.replace(old, new, 1)

    # 2. Add admin state map to GeneratedProxy.
    old = '''    runtime_state: Arc<Mutex<HashMap<String, RuntimeUpstreamState>>>,
'''
    new = '''    runtime_state: Arc<Mutex<HashMap<String, RuntimeUpstreamState>>>,
    admin_upstream_state: Arc<Mutex<HashMap<String, RuntimeAdminUpstreamState>>>,
'''
    if old not in text:
        fail("Could not find GeneratedProxy runtime_state field.")
    text = text.replace(old, new, 1)

    # 3. Create admin state in new().
    old = '''        let runtime_state = Arc::new(Mutex::new(HashMap::new()));

        {
            let mut state = runtime_state.lock().unwrap();
'''
    new = '''        let runtime_state = Arc::new(Mutex::new(HashMap::new()));
        let admin_upstream_state = Arc::new(Mutex::new(HashMap::new()));

        {
            let mut state = runtime_state.lock().unwrap();
            let mut admin_state = admin_upstream_state.lock().unwrap();
'''
    if old not in text:
        fail("Could not find runtime_state initialization.")
    text = text.replace(old, new, 1)

    # 4. Initialize admin state for each upstream.
    old = '''                    state.insert(
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
'''
    new = old + '''
                    admin_state.insert(
                        runtime_state_key(&route.path, &upstream.address),
                        RuntimeAdminUpstreamState {
                            state: upstream.state.clone(),
                            enabled: upstream.enabled,
                            draining: upstream.draining,
                            role: upstream.role.clone(),
                            backup: upstream.backup,
                            updated_unix_ms: current_unix_ms(),
                        },
                    );
'''
    if old not in text:
        fail("Could not find runtime_state upstream insert.")
    text = text.replace(old, new, 1)

    # 5. Add field in proxy constructor.
    old = '''            runtime_state,
'''
    new = '''            runtime_state,
            admin_upstream_state,
'''
    if old not in text:
        fail("Could not find proxy constructor runtime_state field.")
    text = text.replace(old, new, 1)

    # 6. Replace upstream_allows_new_traffic and upstream_is_backup with admin-aware versions.
    old = '''    fn upstream_allows_new_traffic(&self, upstream: &UpstreamConfig) -> bool {
        pingora_ai_upstream_state_enabled(
            &upstream.state,
            upstream.enabled,
            upstream.draining,
        )
    }

    fn upstream_is_backup(&self, upstream: &UpstreamConfig) -> bool {
        pingora_ai_upstream_role_is_backup(
            &upstream.role,
            upstream.backup,
        )
    }
'''
    new = '''    fn admin_state_for_upstream(&self, route: &RouteConfig, upstream: &UpstreamConfig) -> Option<RuntimeAdminUpstreamState> {
        let key = runtime_state_key(&route.path, &upstream.address);

        let state = match self.admin_upstream_state.lock() {
            Ok(guard) => guard,
            Err(_) => return None,
        };

        state.get(&key).cloned()
    }

    fn upstream_allows_new_traffic_for_route(&self, route: &RouteConfig, upstream: &UpstreamConfig) -> bool {
        if let Some(admin) = self.admin_state_for_upstream(route, upstream) {
            return pingora_ai_upstream_state_enabled(
                &admin.state,
                admin.enabled,
                admin.draining,
            );
        }

        pingora_ai_upstream_state_enabled(
            &upstream.state,
            upstream.enabled,
            upstream.draining,
        )
    }

    fn upstream_is_backup_for_route(&self, route: &RouteConfig, upstream: &UpstreamConfig) -> bool {
        if let Some(admin) = self.admin_state_for_upstream(route, upstream) {
            return pingora_ai_upstream_role_is_backup(
                &admin.role,
                admin.backup,
            );
        }

        pingora_ai_upstream_role_is_backup(
            &upstream.role,
            upstream.backup,
        )
    }
'''
    if old not in text:
        fail("Could not find upstream_allows_new_traffic/upstream_is_backup block.")
    text = text.replace(old, new, 1)

    # 7. Update selector calls.
    text = text.replace(
        "if !self.upstream_allows_new_traffic(upstream) {",
        "if !self.upstream_allows_new_traffic_for_route(route, upstream) {",
    )
    text = text.replace(
        "if self.upstream_is_backup(upstream) != want_backup {",
        "if self.upstream_is_backup_for_route(route, upstream) != want_backup {",
    )

    # 8. Add admin methods before serve_runtime_state.
    marker = "    async fn serve_runtime_state(&self, session: &mut Session) -> Result<bool> {"
    admin_methods = r'''
    fn query_param(query: &str, name: &str) -> Option<String> {
        for part in query.split('&') {
            let mut pieces = part.splitn(2, '=');
            let key = pieces.next().unwrap_or("");
            let value = pieces.next().unwrap_or("");

            if key == name {
                return Some(value.replace("%2F", "/").replace("%3A", ":"));
            }
        }

        None
    }

    fn update_admin_upstream_state(&self, action: &str, route_path: &str, upstream_address: &str) -> (bool, String) {
        let normalised = upstream_address
            .replace("127.0.0.1:", "host.docker.internal:")
            .replace("localhost:", "host.docker.internal:");

        let route = match self.routes.iter().find(|route| route.path == route_path) {
            Some(value) => value,
            None => return (false, format!("route_not_found: {}", route_path)),
        };

        let target = match route.upstreams.iter().find(|upstream| {
            upstream.address == upstream_address || upstream.address == normalised
        }) {
            Some(value) => value,
            None => return (false, format!("upstream_not_found: {}", upstream_address)),
        };

        if action == "disable" || action == "drain" {
            let active_primary_count = route.upstreams.iter().filter(|upstream| {
                if upstream.address == target.address {
                    return false;
                }

                self.upstream_allows_new_traffic_for_route(route, upstream)
                    && !self.upstream_is_backup_for_route(route, upstream)
            }).count();

            if active_primary_count < 1 {
                return (false, "cannot_disable_or_drain_last_active_primary".to_string());
            }
        }

        let key = runtime_state_key(&route.path, &target.address);

        let mut state = match self.admin_upstream_state.lock() {
            Ok(guard) => guard,
            Err(_) => return (false, "admin_state_lock_failed".to_string()),
        };

        let entry = state.entry(key).or_insert(RuntimeAdminUpstreamState {
            state: target.state.clone(),
            enabled: target.enabled,
            draining: target.draining,
            role: target.role.clone(),
            backup: target.backup,
            updated_unix_ms: current_unix_ms(),
        });

        match action {
            "disable" => {
                entry.state = "disabled".to_string();
                entry.enabled = false;
                entry.draining = false;
            }
            "drain" => {
                entry.state = "draining".to_string();
                entry.enabled = false;
                entry.draining = true;
            }
            "enable" => {
                entry.state = "enabled".to_string();
                entry.enabled = true;
                entry.draining = false;
            }
            _ => return (false, format!("unknown_action: {}", action)),
        }

        entry.updated_unix_ms = current_unix_ms();

        (true, "updated_in_memory".to_string())
    }

    async fn serve_runtime_admin_upstream(&self, session: &mut Session, action: &str) -> Result<bool> {
        let method = session.req_header().method.as_str().to_uppercase();

        if method != "POST" {
            return self.write_json_response(
                session,
                405,
                "{\"ok\":false,\"error\":\"method_not_allowed\"}".to_string(),
            ).await;
        }

        let query = session.req_header().uri.query().unwrap_or("");
        let confirm = Self::query_param(query, "confirm").unwrap_or_default();

        if confirm != "true" {
            return self.write_json_response(
                session,
                400,
                "{\"ok\":false,\"error\":\"confirm_true_required\"}".to_string(),
            ).await;
        }

        let route = Self::query_param(query, "route").unwrap_or_else(|| "/api".to_string());
        let upstream = Self::query_param(query, "upstream").unwrap_or_default();

        if upstream.is_empty() {
            return self.write_json_response(
                session,
                400,
                "{\"ok\":false,\"error\":\"upstream_required\"}".to_string(),
            ).await;
        }

        let (ok, message) = self.update_admin_upstream_state(action, &route, &upstream);

        let status = if ok { 200 } else { 400 };
        let body = format!(
            "{{\"ok\":{},\"runtime_admin\":true,\"action\":\"{}\",\"route\":\"{}\",\"upstream\":\"{}\",\"message\":\"{}\",\"live_runtime_mutation\":{},\"restart\":false,\"docker_apply\":false}}",
            ok,
            json_escape(action),
            json_escape(&route),
            json_escape(&upstream),
            json_escape(&message),
            ok
        );

        self.write_json_response(session, status, body).await
    }

'''
    if marker not in text:
        fail("Could not find serve_runtime_state marker.")
    text = text.replace(marker, admin_methods + marker, 1)

    # 9. Add request_filter route before runtime_state.
    old = '''        if path == "/__gateway_runtime_state" {
            if method != "GET" && method != "HEAD" {
                session.respond_error(405).await?;
                return Ok(true);
            }

            return self.serve_runtime_state(session).await;
        }
'''
    new = '''        if path == "/__gateway_runtime_admin/upstream/disable" {
            return self.serve_runtime_admin_upstream(session, "disable").await;
        }

        if path == "/__gateway_runtime_admin/upstream/drain" {
            return self.serve_runtime_admin_upstream(session, "drain").await;
        }

        if path == "/__gateway_runtime_admin/upstream/enable" {
            return self.serve_runtime_admin_upstream(session, "enable").await;
        }

''' + old
    if old not in text:
        fail("Could not find runtime_state request_filter block.")
    text = text.replace(old, new, 1)



    # 11. Make runtime_state_json show effective live admin state.
    old_json = '                upstreams_json.push(format!(\n                    "{{\\"address\\":\\"{}\\",\\"weight\\":{},\\"state\\":\\"{}\\",\\"enabled\\":{},\\"draining\\":{},\\"role\\":\\"{}\\",\\"backup\\":{},\\"healthy\\":{},\\"consecutive_successes\\":{},\\"consecutive_failures\\":{},\\"last_status\\":{},\\"last_error\\":{},\\"last_checked_unix_ms\\":{}}}",\n                    json_escape(&upstream.address),\n                    upstream.weight,\n                    json_escape(&upstream.state),\n                    upstream.enabled,\n                    upstream.draining,\n                    json_escape(&upstream.role),\n                    upstream.backup,\n                    item.healthy,\n                    item.consecutive_successes,\n                    item.consecutive_failures,\n                    last_status_json,\n                    last_error_json,\n                    item.last_checked_unix_ms\n                ));\n'

    new_json = '                let admin_item = self.admin_state_for_upstream(route, upstream);\n                let effective_state = admin_item.as_ref().map(|value| value.state.as_str()).unwrap_or(upstream.state.as_str());\n                let effective_enabled = admin_item.as_ref().map(|value| value.enabled).unwrap_or(upstream.enabled);\n                let effective_draining = admin_item.as_ref().map(|value| value.draining).unwrap_or(upstream.draining);\n                let effective_role = admin_item.as_ref().map(|value| value.role.as_str()).unwrap_or(upstream.role.as_str());\n                let effective_backup = admin_item.as_ref().map(|value| value.backup).unwrap_or(upstream.backup);\n                let live_admin_override = admin_item.is_some();\n\n                upstreams_json.push(format!(\n                    "{{\\"address\\":\\"{}\\",\\"weight\\":{},\\"config_state\\":\\"{}\\",\\"effective_state\\":\\"{}\\",\\"state\\":\\"{}\\",\\"enabled\\":{},\\"draining\\":{},\\"role\\":\\"{}\\",\\"backup\\":{},\\"live_admin_override\\":{},\\"healthy\\":{},\\"consecutive_successes\\":{},\\"consecutive_failures\\":{},\\"last_status\\":{},\\"last_error\\":{},\\"last_checked_unix_ms\\":{}}}",\n                    json_escape(&upstream.address),\n                    upstream.weight,\n                    json_escape(&upstream.state),\n                    json_escape(effective_state),\n                    json_escape(effective_state),\n                    effective_enabled,\n                    effective_draining,\n                    json_escape(effective_role),\n                    effective_backup,\n                    live_admin_override,\n                    item.healthy,\n                    item.consecutive_successes,\n                    item.consecutive_failures,\n                    last_status_json,\n                    last_error_json,\n                    item.last_checked_unix_ms\n                ));\n'

    if old_json in text:
        text = text.replace(old_json, new_json, 1)
    elif "effective_state" in text and "live_admin_override" in text:
        pass
    else:
        fail("Could not patch runtime_state_json effective admin state.")

    # 10. Add admin state to runtime_state JSON simple by adding note/version at top.
    text = text.replace(
        '"{\\"ok\\":true,\\"health_check\\":',
        '"{\\"ok\\":true,\\"runtime_admin_phase1\\":true,\\"health_check\\":',
        1,
    )

    MAIN_RS.write_text(text, encoding="utf-8")
    print("✅ Runtime Admin Agent phase1 patched into generated Rust.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
