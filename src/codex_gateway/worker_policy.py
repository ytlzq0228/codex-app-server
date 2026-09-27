"""Server-owned policy: the gateway is inference + client tools, never remote exec."""
# Every turn disables environment access, independently of client prompts.
# Config switches remove secondary native execution/plugin surfaces as well.
FEATURES = ['shell_tool','unified_exec','shell_snapshot','code_mode','code_mode_host',
            'code_mode_only','multi_agent','multi_agent_v2','apps','plugins','hooks',
            'browser_use','browser_use_external','computer_use','in_app_browser',
            'image_generation','view_image','skill_mcp_dependency_install','skill_search',
            'workspace_dependencies','worktrees','request_permissions_tool']
CONFIG = {**{f'features.{name}':False for name in FEATURES},
          'web_search':'disabled','project_doc_max_bytes':0,'mcp_servers':{},
          'apps._default.enabled':False}


def thread_policy():
    return {'environments':[], 'sandbox':'read-only','approvalPolicy':'untrusted',
            'approvalsReviewer':'user','config':dict(CONFIG),'selectedCapabilityRoots':[]}


def turn_policy():
    return {'environments':[], 'approvalPolicy':'untrusted','approvalsReviewer':'user',
            'sandboxPolicy':{'type':'readOnly','networkAccess':False}}
