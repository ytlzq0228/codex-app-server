<?php
require_once('/etc/inc/config.inc');
require_once('/usr/local/pkg/haproxy/haproxy.inc');
$apply = in_array('--apply', $argv, true);
// Usage: php pfsense-codex-ha.php --nodes=<app-1-ip>,<app-2-ip> [--apply]
$nodes = [];
foreach ($argv as $arg) {
    if (strpos($arg, '--nodes=') === 0) $nodes = array_values(array_filter(explode(',', substr($arg, 8))));
}
foreach ($nodes as $address) {
    if (!filter_var($address, FILTER_VALIDATE_IP)) { fwrite(STDERR, "Invalid node address: $address\n"); exit(1); }
}
if (count($nodes) !== 2) { fwrite(STDERR, "Pass --nodes=<app-1-ip>,<app-2-ip>\n"); exit(1); }
$found = false;
foreach ($config['installedpackages']['haproxy']['ha_pools']['item'] as &$pool) {
    if ($pool['name'] !== 'codex-app-server-backend') continue;
    $old = $pool['ha_servers']['item'];
    $new = [];
    foreach ($nodes as $i => $address) {
        $existing = null;
        foreach ($old as $server) {
            if ($server['address'] === $address && $server['port'] === '8000') $existing = $server;
        }
        $server = $existing ?? ['id' => (string)(115 + $i)];
        $server['status'] = 'active';
        $server['name'] = 'codex-app-server-' . implode('.', array_slice(explode('.', $address), -2));
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
    write_config('Codex dual-active APP backend ' . implode(' + ', $nodes) . ' -NoReMoTeBaCkUp');
    if (!haproxy_check_and_run($messages, true)) {
        fwrite(STDERR, "HAProxy reload failed; saved configuration backup retained\n"); exit(3);
    }
}
echo json_encode(['mode' => $apply ? 'applied' : 'validated', 'backend' => 'codex-app-server-backend', 'nodes' => array_map(fn($a) => $a . ':8000', $nodes)]) . "\n";
