

def heal_environment(environment: str = "local", public_url: str | None = None) -> dict:
    """
    Environment-level self-healing:
    scan -> classify -> deterministic safe repair -> rescan.
    """
    from core.environment_guardian import scan_environment
    from core.environment_repairs import repair_public_edge

    before = scan_environment(environment=environment, public_url=public_url)
    repairs = []

    failed_names = {c.get("name") for c in before.get("failed_checks", [])}

    if {
        "nginx_active",
        "nginx_listening_public_ports",
        "iptables_80_443_before_reject",
    } & failed_names:
        repairs.append(repair_public_edge())

    after = scan_environment(environment=environment, public_url=public_url)

    return {
        "success": after.get("success", False),
        "environment": environment,
        "before": before,
        "repairs": repairs,
        "after": after,
    }
