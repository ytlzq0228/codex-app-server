<?php
require_once('/etc/inc/config.inc');
require_once('/usr/local/pkg/haproxy/haproxy.inc');
$apply = in_array('--apply', $argv, true);
$found = false;
foreach ($config['installedpackages']['haproxy']['ha_pools']['item'] as &$pool) {
    if ($pool['name'] !== 'codex-app-server-backend') continue;
    $old = $pool['ha_servers']['item'];
    $new = [];
    foreach (['<app-1>', '<app-2>'] as $i => $address) {
        $existing = null;
        foreach ($old as $server) {
            if ($server['address'] === $address && $server['port'] === '8000') $existing = $server;
        }
        $server = $existing ?? ['id' => (string)(115 + $i)];
        $server['status'] = 'active';
        $server['name'] = 'codex-app-server-' . substr($address, 7);
        $server['address'] = $address;
        $server['port'] = '8000';
        $new[] = $server;
    }
    $pool['ha_servers']['item'] = $new;
    $pool['balance'] = 'leastconn';
    $pool['check_type'] = 'HTTP';
    $pool['httpcheck_method'] = 'GET';
    $pool['monitor_uri'] = '/healthz';
    $pool['checkinter'] = '2000';
    $rules = ['default-server fall 3 rise 2', 'http-request deny deny_status 404 if { path_beg /internal/ }'];
    $advanced = base64_decode($pool['advanced_backend'] ?? '');
    foreach ($rules as $rule) if (strpos($advanced, $rule) === false) $advanced .= "\n" . $rule;
    $pool['advanced_backend'] = base64_encode(trim($advanced));
    $found = true;
}
unset($pool);
if (!$found) { fwrite(STDERR, "Codex production backend not found\n"); exit(1); }
$messages = '';
if (!haproxy_check_and_run($messages, false)) {
    fwrite(STDERR, "HAProxy candidate validation failed\n"); exit(2);
}
if ($apply) {
    if (!file_exists('/conf/config.xml.before-codex-ha-20261002')) {
        copy('/conf/config.xml', '/conf/config.xml.before-codex-ha-20261002');
        chmod('/conf/config.xml.before-codex-ha-20261002', 0600);
    }
    write_config('Codex dual-active APP backend <app-1> + <app-2> -NoReMoTeBaCkUp');
    if (!haproxy_check_and_run($messages, true)) {
        fwrite(STDERR, "HAProxy reload failed; saved configuration backup retained\n"); exit(3);
    }
}
echo json_encode(['mode' => $apply ? 'applied' : 'validated', 'backend' => 'codex-app-server-backend', 'nodes' => ['<app-1>:8000', '<app-2>:8000']]) . "\n";
