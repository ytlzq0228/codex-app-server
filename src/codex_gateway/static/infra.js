(() => {
  const nodes = document.getElementById('infra-nodes');
  if (!nodes) return;
  const refresh = document.getElementById('infra-refresh');
  const updated = document.getElementById('infra-updated');
  const escape = value => String(value ?? '—').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const bytes = n => n == null ? '—' : n >= 1073741824 ? (n / 1073741824).toFixed(2) + ' GiB' : (n / 1048576).toFixed(1) + ' MiB';
  const percent = n => n == null ? '—' : n.toFixed(1) + '%';
  const time = n => n ? new Date(n).toLocaleString() : '—';
  const badge = (ok, label) => `<span class="badge ${ok ? 'badge-ok' : 'badge-off'}">${escape(label)}</span>`;
  const metric = (label, value) => `<div class="infra-metric"><span>${escape(label)}</span><strong>${escape(value)}</strong></div>`;
  let busy = false;
  async function load() {
    if (busy) return;
    busy = true; refresh.disabled = true;
    try {
      const response = await fetch('/infra/status', {cache:'no-store', headers:{Accept:'application/json'}});
      if (!response.ok || !response.headers.get('content-type')?.includes('application/json')) throw new Error('无法读取状态，请检查连接或重新登录。');
      const data = await response.json();
      const db = data.database;
      document.getElementById('infra-database').innerHTML = `${badge(true, db.replica ? '只读副本' : '主库在线')} <span>${escape(db.name)} · ${escape(db.address)} · ${bytes(db.size_bytes)}</span>`;
      nodes.innerHTML = data.nodes.map(node => {
        const docker = node.docker;
        const byName = new Map((docker?.containers || []).map(c => [c.name,c]));
        const rows = node.workers.map(worker => {
          const c = byName.get(worker.container_name);
          return `<tr><td>${escape(worker.name)}</td><td>${escape(worker.status)}</td><td>${escape(c?.state || (docker ? '未找到容器' : '无法读取'))}</td><td>${percent(c?.cpu_percent)}</td><td>${bytes(c?.memory_bytes)}</td></tr>`;
        }).join('');
        return `<article class="panel infra-node"><div class="section-head"><div><h3>${escape(node.id)}</h3><span class="muted">${escape(node.address)}${data.current_node === node.id ? ' · 当前接入节点' : ''}</span></div>${badge(node.enabled && node.gateway_status === 'online' && node.manager_status === 'online' && node.heartbeat_fresh !== false, node.gateway_status === 'online' && node.manager_status === 'online' && node.enabled && node.heartbeat_fresh !== false ? '在线' : '异常 / 降级')}</div>
        <p class="infra-meta">APP：${node.gateway_status === 'online' ? '在线' : '不可用'} · Worker 管理：${node.manager_status === 'online' ? '在线' : '不可用'} · ${node.enabled ? '允许调度' : '已停用'}<br>心跳：${escape(time(node.heartbeat_at))}${node.heartbeat_fresh === false ? '（已过期）' : ''}</p>
        ${docker ? `<div class="infra-metrics">${metric('Docker CPU / '+docker.cpu_cores+' 核',percent(docker.cpu_percent))}${metric('Docker 内存 / '+bytes(docker.memory_total_bytes),bytes(docker.memory_bytes))}${metric('容器运行 / 停止',docker.running+' / '+docker.stopped)}${metric('Worker / 活跃连接',node.workers.length+' / '+(node.connections ?? '—'))}</div><p class="infra-meta">${escape(docker.hostname)} · Docker ${escape(docker.docker_version)}<br>负载采样：${escape(time(docker.sampled_at))}</p>` : '<p class="infra-alert">无法读取该节点的 Docker 负载。该节点 Worker 可能暂时不可用。</p>'}
        <div class="table-wrap"><table><thead><tr><th>Worker</th><th>调度状态</th><th>容器状态</th><th>CPU</th><th>内存</th></tr></thead><tbody>${rows || '<tr><td colspan="5" class="empty">没有 Worker</td></tr>'}</tbody></table></div></article>`;
      }).join('') || '<p class="infra-alert">没有已注册的 APP 节点。</p>';
      updated.textContent = '最后刷新：'+time(data.timestamp); updated.classList.remove('infra-alert');
    } catch (error) {
      updated.textContent = error.message + (nodes.children.length ? ' 页面保留上次数据，请以刷新时间为准。' : '');
      updated.classList.add('infra-alert');
    } finally { busy = false; refresh.disabled = false; }
  }
  refresh.addEventListener('click', load); load();
  setInterval(() => { if (!document.hidden) load(); }, 30000);
})();
