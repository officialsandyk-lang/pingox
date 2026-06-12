from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GENERATED_MAIN = ROOT / "generated-pingora-proxy" / "src" / "main.rs"
ADMIN_PATCH = ROOT / "scripts" / "patch_runtime_admin_agent_phase1.py"

OBSOLETE_HELPERS = [
    ROOT / "scripts" / "patch_generated_runtime_upstream_state.py",
    ROOT / "scripts" / "patch_generated_runtime_upstream_state_wire.py",
]


def test_generated_runtime_has_runtime_admin_upstream_state() -> None:
    text = GENERATED_MAIN.read_text(encoding="utf-8")

    required = [
        "struct RuntimeAdminUpstreamState",
        "admin_upstream_state: Arc<Mutex<HashMap<String, RuntimeAdminUpstreamState>>>",
        "fn update_admin_upstream_state",
        "fn admin_state_for_upstream",
        "effective_state",
        "live_admin_override",
        "config_state",
        "runtime_state_key",
    ]

    missing = [marker for marker in required if marker not in text]
    assert not missing, f"generated runtime missing admin markers: {missing}"


def test_runtime_admin_generation_patch_is_current_source_of_truth() -> None:
    text = ADMIN_PATCH.read_text(encoding="utf-8")

    required = [
        "struct RuntimeAdminUpstreamState",
        "admin_upstream_state",
        "update_admin_upstream_state",
        "admin_state_for_upstream",
        "effective_state",
        "live_admin_override",
    ]

    missing = [marker for marker in required if marker not in text]
    assert not missing, f"runtime admin patch missing markers: {missing}"


def test_obsolete_generated_runtime_helper_scripts_are_not_restored() -> None:
    existing = [str(path.relative_to(ROOT)) for path in OBSOLETE_HELPERS if path.exists()]
    assert not existing, f"obsolete generated-runtime helper scripts should not exist: {existing}"

def test_template_has_runtime_admin_upstream_state() -> None:
    template = ROOT / "templates" / "pingora_runtime" / "src" / "main.rs"
    text = template.read_text(encoding="utf-8")

    required = [
        "PINGORA_AI_RUNTIME_ADMIN_TEMPLATE_INTEGRATED_V1",
        "struct RuntimeAdminUpstreamState",
        "admin_upstream_state: Arc<Mutex<HashMap<String, RuntimeAdminUpstreamState>>>",
        "fn update_admin_upstream_state",
        "async fn serve_runtime_admin_upstream",
        "fn admin_state_for_upstream",
        "fn upstream_allows_new_traffic_for_route",
        "fn upstream_is_backup_for_route",
        "fn eligible_upstream_indices_by_role",
        "effective_state",
        "live_admin_override",
        "/__gateway_runtime_admin/upstream/disable",
        "/__gateway_runtime_admin/upstream/drain",
        "/__gateway_runtime_admin/upstream/enable",
    ]

    missing = [marker for marker in required if marker not in text]
    assert not missing, f"template missing runtime admin markers: {missing}"


def test_runtime_admin_patch_script_is_template_compatibility_only() -> None:
    text = ADMIN_PATCH.read_text(encoding="utf-8")

    required = [
        "Runtime admin already template-integrated; compatibility patch skipped.",
        "template_integrated_markers",
        "async fn serve_runtime_admin_upstream",
        "fn eligible_upstream_indices_by_role",
        "live_admin_override",
    ]

    missing = [marker for marker in required if marker not in text]
    assert not missing, f"runtime admin patch script missing compatibility markers: {missing}"

