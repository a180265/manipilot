import React, { useCallback, useEffect, useState } from "react";
import { callApi, opContext } from "../api";
import { ErrorBanner, ReasonDialog, useApp } from "../ui";

// ---------- 能力与插件 ----------
export const CapabilitiesPlugins: React.FC = () => {
  const { version } = useApp();
  const [capabilities, setCapabilities] = useState<any[]>([]);
  const [plugins, setPlugins] = useState<any[]>([]);
  const [error, setError] = useState<any>(null);
  const [dialog, setDialog] = useState<{ plugin: string; action: string } | null>(null);
  const [installDir, setInstallDir] = useState("");

  const load = useCallback(async () => {
    try {
      setCapabilities((await callApi("/v1/capabilities")).capabilities);
      setPlugins((await callApi("/v1/plugins")).plugins);
    } catch (e: any) {
      setError(e);
    }
  }, [version]);

  useEffect(() => {
    load();
  }, [load]);

  const runPluginAction = async (pluginId: string, action: string, reason: string) => {
    const dangerous = action === "uninstall" || action === "upgrade" || action === "rollback";
    try {
      await callApi(`/v1/plugins/${pluginId}/${action}`, {
        method: "POST",
        body: { ...opContext(reason, { dangerous }), source_dir: action === "install" || action === "upgrade" ? installDir : undefined },
      });
      setDialog(null);
      load();
    } catch (e: any) {
      setError(e);
      setDialog(null);
    }
  };

  const actionTitles: Record<string, string> = {
    activate: "启用插件",
    deactivate: "停用插件",
    uninstall: "卸载插件",
    rollback: "回滚插件",
  };

  return (
    <div className="page">
      <div className="page-head"><h2>能力与插件</h2></div>
      <ErrorBanner error={error} onClose={() => setError(null)} />
      <section className="panel">
        <h3>能力清单</h3>
        <table className="table">
          <thead><tr><th>能力编号</th><th>中文名称</th><th>版本</th><th>风险等级</th><th>数据范围</th><th>来源</th></tr></thead>
          <tbody>
            {capabilities.map((capability) => (
              <tr key={capability.capability_id}>
                <td className="mono">{capability.capability_id}</td>
                <td>{capability.name_display}</td>
                <td>{capability.version}</td>
                <td>{capability.risk_level}</td>
                <td>{capability.data_scope}</td>
                <td>{String(capability.source).startsWith("plugin:") ? `插件 ${String(capability.source).slice(7)}` : "平台"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      <section className="panel">
        <h3>外部插件</h3>
        <p className="muted">平台核心不内置任何具体应用业务；拍照、录屏、刷视频、桌面巡检、RPG、微信适配等都由外部插件按此机制安装、审查和卸载。</p>
        <table className="table">
          <thead><tr><th>插件编号</th><th>名称</th><th>版本</th><th>状态</th><th>任务类型</th><th>操作</th></tr></thead>
          <tbody>
            {plugins.map((plugin) => (
              <tr key={plugin.plugin_id}>
                <td className="mono">{plugin.plugin_id}</td>
                <td>{plugin.name_display}</td>
                <td>{plugin.version}</td>
                <td>{plugin.status_display}</td>
                <td>{(plugin.manifest?.task_types ?? []).map((t: any) => `${t.name_display}（${t.task_type}）`).join("、")}</td>
                <td>
                  {plugin.status !== "activated" && <button onClick={() => setDialog({ plugin: plugin.plugin_id, action: "activate" })}>启用</button>}
                  {plugin.status === "activated" && <button onClick={() => setDialog({ plugin: plugin.plugin_id, action: "deactivate" })}>停用</button>}
                  <button onClick={() => setDialog({ plugin: plugin.plugin_id, action: "uninstall" })}>卸载</button>
                  {plugin.status === "activated" && <button onClick={() => setDialog({ plugin: plugin.plugin_id, action: "rollback" })}>回滚</button>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="install-line">
          <h3>安装插件（服务器本地目录）</h3>
          <input placeholder="插件源目录绝对路径（含 manifest.json）" value={installDir} onChange={(e) => setInstallDir(e.target.value)} />
          <button
            disabled={!installDir.trim()}
            onClick={async () => {
              try {
                await callApi("/v1/plugins/install", { method: "POST", body: { ...opContext("安装新插件"), source_dir: installDir.trim() } });
                setInstallDir("");
                load();
              } catch (e: any) {
                setError(e);
              }
            }}
          >
            安装
          </button>
        </div>
      </section>
      {dialog && (
        <ReasonDialog
          title={`${actionTitles[dialog.action]}：${dialog.plugin}`}
          dangerous={dialog.action === "uninstall" || dialog.action === "rollback"}
          onSubmit={(reason) => runPluginAction(dialog.plugin, dialog.action, reason)}
          onCancel={() => setDialog(null)}
        />
      )}
    </div>
  );
};

// ---------- 审计日志 ----------
export const Audit: React.FC = () => {
  const { version } = useApp();
  const [events, setEvents] = useState<any[]>([]);
  const [error, setError] = useState<any>(null);

  useEffect(() => {
    callApi("/v1/audit-events?limit=200")
      .then((data) => setEvents(data.events))
      .catch((e) => setError(e));
  }, [version]);

  return (
    <div className="page">
      <div className="page-head"><h2>审计日志</h2></div>
      <ErrorBanner error={error} onClose={() => setError(null)} />
      <table className="table">
        <thead><tr><th>时间</th><th>动作</th><th>操作者</th><th>设备</th><th>任务</th><th>原因</th><th>结果</th></tr></thead>
        <tbody>
          {events.map((event, index) => (
            <tr key={index}>
              <td>{event.occurred_at}</td>
              <td>{event.action_display}</td>
              <td>{event.operator_id}</td>
              <td>{event.device_id ?? "—"}</td>
              <td>{event.task_id ?? "—"}</td>
              <td>{event.reason ?? "—"}</td>
              <td>{event.outcome_display}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
};

// ---------- 运行设置 ----------
export const Settings: React.FC = () => {
  const { role, setRole, operatorId, setOperatorId, connected, lastEventAt } = useApp();
  const [status, setStatus] = useState<any>(null);
  const [error, setError] = useState<any>(null);

  const refresh = useCallback(() => {
    callApi("/v1/system/status").then(setStatus).catch((e) => setError(e));
  }, []);

  useEffect(() => {
    refresh();
    const timer = window.setInterval(refresh, 5000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const estop = async () => {
    if (!confirm("确认触发全局急停？平台将立即停止领取新任务并通知 WX 暂停调度。")) return;
    try {
      await callApi("/v1/emergency-stop", { method: "POST", body: opContext("控制台触发全局急停", { dangerous: true }) });
      callApi("/v1/system/status").then(setStatus);
    } catch (e: any) {
      setError(e);
    }
  };

  const resetEstop = async () => {
    if (!confirm("确认复位急停？请先完成现场人工复位。")) return;
    try {
      await callApi("/v1/emergency-stop/reset", { method: "POST", body: opContext("现场已排除风险，人工复位急停", { dangerous: true }) });
      callApi("/v1/system/status").then(setStatus);
    } catch (e: any) {
      setError(e);
    }
  };

  return (
    <div className="page">
      <div className="page-head"><h2>运行设置</h2></div>
      <ErrorBanner error={error} onClose={() => setError(null)} />
      <section className="panel">
        <h3>连接与版本</h3>
        <div>连接状态：{connected ? "实时推送已连接" : `连接已断开（${lastEventAt ? `最后更新 ${lastEventAt}` : "无数据"}）`}</div>
        <div>平台版本：{status?.platform_version} · 硬件模式：{status?.hardware_mode_display}</div>
        <div>契约版本：{status?.contract_version} · 急停状态：{status?.estop_active_display}</div>
        <div>已注册任务类型：{(status?.task_type_registry ?? []).map((t: any) => t.name_display).join("、") || "无（未安装插件）"}</div>
      </section>
      <section className="panel">
        <h3>操作者与角色</h3>
        <label>
          操作者：
          <input value={operatorId} onChange={(e) => setOperatorId(e.target.value)} />
        </label>
        <label>
          角色：
          <select value={role} onChange={(e) => setRole(e.target.value)}>
            <option value="readonly">只读</option>
            <option value="operator">运维</option>
            <option value="responder">应急</option>
          </select>
        </label>
        <p className="muted">前端只隐藏无权限操作，后端会再次拒绝越权请求。</p>
      </section>
      <section className="panel">
        <h3>全局急停</h3>
        <div className="card-actions">
          <button className="danger" onClick={estop}>触发全局急停</button>
          <button onClick={resetEstop}>急停复位（需现场确认）</button>
        </div>
      </section>
    </div>
  );
};
