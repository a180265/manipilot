/// <reference types="vite/client" />
import React, { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { callApi, opContext, currentOperator } from "../api";
import { ErrorBanner, ReasonDialog, StatusTag, useApp } from "../ui";
import { PrepareRun } from "../PrepareRun";
import {
  bindingDraftDirty, canDeviceAction, createLatestRequest, createPreviewOwner,
  debugConsoleUrl, newBindingDraft, refreshBindingEditor,
} from "./device-state";
import type { BindingDraft } from "./device-state";

interface Device {
  device_id: string;
  name_display: string;
  status: string;
  status_display: string;
  status_reason?: string | null;
  presence_state?: string;
  driver_mode?: string;
  binding_mode?: string;
  daemon_session?: string | null;
  calibration_status_display?: string;
  current_ui_state_display: string;
  wechat_account_id: string;
  camera_id: string;
  camera_quality_display: string;
  camera_source_display?: string | null;
  camera_last_frame_at?: string | null;
  camera_frame_age_ms?: number | null;
  queue_length: number;
  last_heartbeat_at: string | null;
  lease_id: string | null;
  home_ok: boolean;
  binding_note: string;
  binding_revision?: number;
  binding_enabled?: boolean;
}

interface BindingView {
  binding: Record<string, any>;
  binding_revision: number;
  config_version: string;
  binding_enabled: boolean;
  binding_enabled_display: string;
  daemon_session?: string | null;
  calibration_status_display?: string;
  field_descriptions: Record<string, string>;
  recent_audit?: { occurred_at: string; action_display: string; operator_id?: string; reason?: string }[];
}

interface BindingResourceOption {
  resource_id: string;
  label: string;
  selected?: boolean;
}

interface BindingOptions {
  camera_options: BindingResourceOption[];
  daemon_options: BindingResourceOption[];
  serial_options: BindingResourceOption[];
  说明?: string;
}

interface BindingEditor {
  base: BindingView;
  options: BindingOptions;
  draft: BindingDraft;
}

const BINDING_FIELDS: { key: string; label: string; readOnly?: boolean }[] = [
  { key: "arm_slot", label: "机械臂槽位" },
  { key: "camera_id", label: "摄像头编号", readOnly: true },
  { key: "camera_device_unique_id", label: "物理摄像头唯一 ID", readOnly: true },
  { key: "ime_device_id", label: "输入法设备编号" },
  { key: "wechat_account_id", label: "微信账号编号" },
  { key: "daemon_addr", label: "daemon 地址", readOnly: true },
  { key: "serial_port", label: "串口", readOnly: true },
  { key: "camera_frame_url", label: "摄像头取流地址", readOnly: true },
  { key: "camera_status_url", label: "摄像头状态地址", readOnly: true },
];

const DebugLink: React.FC<{ deviceId: string }> = ({ deviceId }) => {
  const href = debugConsoleUrl(deviceId, import.meta.env.VITE_DEBUG_CONSOLE_URL || undefined);
  return href
    ? <a href={href} target="_blank" rel="noopener noreferrer">打开 {deviceId} 调试台</a>
    : <span className="muted">调试台地址配置无效</span>;
};

const DeviceConnection: React.FC<{ device: Device }> = ({ device }) => (
  <>
    <div>设备在位：{device.presence_state === "present" ? "在位" : device.presence_state === "removed" ? "已移除" : "待确认"}</div>
    <div>机械臂连接：{device.binding_mode === "simulation" || device.driver_mode === "simulation"
      ? "仿真设备（非物理连接）"
      : device.status === "offline" || device.presence_state === "removed"
        ? "离线"
        : device.daemon_session ? "daemon 会话已建立" : "连接未确认"}</div>
    <div>校准状态：{device.calibration_status_display ?? "待确认"}</div>
  </>
);

const DEVICE_ACTION_TITLES: Record<string, string> = {
  pause: "暂停设备",
  resume: "恢复设备",
  quarantine: "隔离设备",
  unquarantine: "解除隔离",
  "home-check": "归位检查",
  "status-query": "人工状态查询",
};

export const Overview: React.FC = () => {
  const { version, connected } = useApp();
  const [devices, setDevices] = useState<Device[]>([]);
  const [error, setError] = useState<any>(null);
  const [dialog, setDialog] = useState<{ device: Device; action: string } | null>(null);
  const navigate = useNavigate();
  const [requests] = useState(createLatestRequest);

  const load = useCallback(async () => {
    const current = requests.begin();
    try {
      const data = await callApi<{ devices: Device[] }>("/v1/devices");
      if (current()) setDevices(data.devices);
    } catch (e: any) {
      if (current()) setError(e);
    }
  }, [requests]);

  useEffect(() => () => requests.invalidate(), [requests]);

  useEffect(() => {
    load();
  }, [load, version]);

  const runAction = async (device: Device, action: string, reason: string) => {
    const dangerous = action === "quarantine" || action === "unquarantine";
    try {
      await callApi(`/v1/devices/${device.device_id}/${action}`, {
        method: "POST",
        body: opContext(reason, { dangerous }),
      });
      setDialog(null);
      await load();
    } catch (e: any) {
      setError(e);
    }
  };

  const estop = async () => {
    try {
      await callApi("/v1/emergency-stop", { method: "POST", body: opContext("操作者触发了全局急停", { dangerous: true }) });
      await load();
    } catch (e: any) {
      setError(e);
    }
  };

  return (
    <div className="page">
      <div className="page-head">
        <h2>设备总览（{devices.length} 台）</h2>
        <div className="head-actions">
          {!connected && <span className="disconnected">连接已断开，数据非实时</span>}
          <button className="danger" onClick={estop}>全局急停</button>
        </div>
      </div>
      <ErrorBanner error={error} onClose={() => setError(null)} />
      <div className="device-grid">
        {devices.map((device) => (
          <div className="device-card" key={device.device_id}>
            <div className="card-title">
              <Link to={`/devices/${device.device_id}`}>{device.name_display}</Link>
              <StatusTag display={device.status_display} />
            </div>
            <div className="card-body">
              <div>微信账号：{device.wechat_account_id || "待确认"}</div>
              <div>摄像头：{device.camera_id}（{device.camera_quality_display}）</div>
              <div>当前界面：{device.current_ui_state_display}</div>
              <DeviceConnection device={device} />
              <div>队列长度：{device.queue_length}</div>
              <div>最近心跳：{device.last_heartbeat_at ?? "—"}</div>
              <div>归位状态：{device.home_ok ? "已归位" : "未确认归位"}</div>
              {device.lease_id && <div className="lease-line">当前有任务租约</div>}
            </div>
            <div className="card-actions">
              <button onClick={() => navigate(`/devices/${device.device_id}`)}>查看详情</button>
              <button onClick={() => setDialog({ device, action: "pause" })} disabled={!canDeviceAction(device.status, "pause", device.home_ok)}>暂停</button>
              <button onClick={() => setDialog({ device, action: "resume" })} disabled={!canDeviceAction(device.status, "resume", device.home_ok)}>恢复</button>
              <button onClick={() => setDialog({ device, action: "quarantine" })} disabled={!canDeviceAction(device.status, "quarantine", device.home_ok)}>隔离</button>
              <button onClick={() => setDialog({ device, action: "unquarantine" })} disabled={!canDeviceAction(device.status, "unquarantine", device.home_ok)}>解除隔离</button>
              <DebugLink deviceId={device.device_id} />
            </div>
            <PrepareRun deviceId={device.device_id} onPrepared={load} />
          </div>
        ))}
      </div>
      {dialog && (
        <ReasonDialog
          title={`${DEVICE_ACTION_TITLES[dialog.action]}：${dialog.device.name_display}`}
          dangerous={dialog.action === "quarantine" || dialog.action === "unquarantine"}
          onSubmit={(reason) => runAction(dialog.device, dialog.action, reason)}
          onCancel={() => setDialog(null)}
        />
      )}
    </div>
  );
};

interface TaskItem {
  task_id: string;
  task_type_display: string;
  status_display: string;
  status: string;
  created_at: string;
  uncertainty_reason?: string;
}

const CameraPreview: React.FC<{ device: Device }> = ({ device }) => {
  const [viewing, setViewing] = useState(false);
  const [starting, setStarting] = useState(false);
  const [viewStatus, setViewStatus] = useState<string>("");
  const [frameAt, setFrameAt] = useState<string>("");
  const [frameUrl, setFrameUrl] = useState<string>("");
  const [owner] = useState(createPreviewOwner);
  const mounted = useRef(false);
  const frameTimer = useRef<number | null>(null);
  const renewTimer = useRef<number | null>(null);
  const frameObjectUrl = useRef<string | null>(null);
  const frameRequest = useRef<AbortController | null>(null);

  const clearFrame = useCallback(() => {
    if (frameObjectUrl.current) {
      URL.revokeObjectURL(frameObjectUrl.current);
      frameObjectUrl.current = null;
    }
    if (mounted.current) {
      setFrameUrl("");
      setFrameAt("");
    }
  }, []);

  const closeSession = async (id: string) => {
    try {
      await callApi(`/v1/camera/view-sessions/${encodeURIComponent(id)}`, {
        method: "DELETE",
        body: { requested_by: currentOperator().operatorId, reason: "结束只读查看" },
      });
    } catch {
      /* 未确认关闭的查看会话由后端超时清理，不重试创建。 */
    }
  };

  const stopView = useCallback(async () => {
    const id = owner.stop();
    if (frameTimer.current !== null) window.clearInterval(frameTimer.current);
    if (renewTimer.current !== null) window.clearInterval(renewTimer.current);
    frameTimer.current = null;
    renewTimer.current = null;
    frameRequest.current?.abort();
    frameRequest.current = null;
    clearFrame();
    if (mounted.current) {
      setStarting(false);
      setViewing(false);
      setViewStatus("");
    }
    if (id) await closeSession(id);
  }, [owner, clearFrame]);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      void stopView();
    };
  }, [stopView]);

  const startView = async () => {
    if (!mounted.current) return;
    const token = owner.begin();
    if (token === null) return;
    const current = () => mounted.current && owner.current(token);
    setStarting(true);
    setViewStatus("");
    try {
      // Do not abort creation: its late response is needed to close the server session.
      const session = await callApi<any>("/v1/camera/view-sessions", {
        method: "POST",
        body: {
          device_id: device.device_id,
          camera_id: device.camera_id,
          purpose: "主动查看",
          requested_by: currentOperator().operatorId,
          correlation_id: `corr-view-${Date.now()}`,
          client_request_id: `req-view-${Date.now()}`,
        },
      });
      const id = session.view_session_id;
      if (typeof id !== "string" || !id) throw new Error("查看会话编号缺失");
      if (!current()) {
        await closeSession(id);
        return;
      }
      if (session.device_id !== device.device_id || session.camera_id !== device.camera_id) {
        await closeSession(id);
        throw new Error("查看会话与当前设备或摄像头不一致");
      }
      if (!owner.adopt(token, id)) {
        await closeSession(id);
        return;
      }
      setViewing(true);
      setStarting(false);
      setViewStatus("正在等待画面");
      const path = `/v1/camera/view-sessions/${encodeURIComponent(id)}`;
      let framePending = false;
      const refresh = async () => {
        if (!current() || framePending) return;
        framePending = true;
        const controller = new AbortController();
        frameRequest.current = controller;
        try {
          const response = await fetch(`${path}/frame`, { signal: controller.signal });
          if (response.ok) {
            const blob = await response.blob();
            if (!current()) return;
            const nextUrl = URL.createObjectURL(blob);
            if (frameObjectUrl.current) URL.revokeObjectURL(frameObjectUrl.current);
            frameObjectUrl.current = nextUrl;
            setFrameUrl(nextUrl);
            setFrameAt(response.headers.get("X-Frame-At") ?? "");
            setViewStatus(session.status_display ?? "有效");
          } else {
            const payload = await response.json().catch(() => null);
            if (!current()) return;
            clearFrame();
            setViewStatus(payload?.error?.title ?? "画面不可用");
          }
        } catch {
          if (current()) {
            clearFrame();
            setViewStatus("画面不可用");
          }
        } finally {
          framePending = false;
          if (frameRequest.current === controller) frameRequest.current = null;
        }
      };
      frameTimer.current = window.setInterval(refresh, 800);
      let renewPending = false;
      renewTimer.current = window.setInterval(async () => {
        if (!current() || renewPending) return;
        renewPending = true;
        try {
          await callApi(`${path}/renew`, { method: "POST", body: { requested_by: currentOperator().operatorId } });
        } catch {
          if (current()) setViewStatus("会话即将结束，续期失败");
        } finally {
          renewPending = false;
        }
      }, 20000);
      void refresh();
    } catch (e: any) {
      if (current()) {
        void stopView();
        setViewStatus(e?.title || e?.message || "无法创建查看会话");
      }
    }
  };

  return (
    <section className="panel">
      <h3>实时画面（只读）</h3>
      {!viewing ? (
        <div>
          <p className="muted">初始只显示最近证据；查看会话不获取任务锁、不暂停任务、不默认录制。</p>
          <button onClick={startView} disabled={starting}>{starting ? "正在建立查看…" : "查看实时画面"}</button>
          {starting && <button onClick={() => void stopView()}>取消查看</button>}
          {viewStatus && <div role="status">{viewStatus}</div>}
        </div>
      ) : (
        <div className="live-view">
          <div className="live-frame" style={{ width: 336, maxWidth: "100%", aspectRatio: "4 / 3" }}>
            {frameUrl && <img src={frameUrl} alt={`${device.device_id} 实时画面`} width={320}
              style={{ display: "block", width: "100%", height: "100%", objectFit: "contain" }} />}
            <span className="live-badge" style={{ maxWidth: "calc(100% - 28px)", overflowWrap: "anywhere", borderRadius: 4 }}>
              {frameUrl ? "实时" : "无可用画面"} {device.device_id} / {device.camera_id} · {frameAt || "…"}
            </span>
          </div>
          <div>会话状态：{viewStatus}</div>
          <button onClick={() => void stopView()}>关闭查看</button>
        </div>
      )}
    </section>
  );
};

export const DeviceDetail: React.FC = () => {
  const { deviceId } = useParams();
  return deviceId ? <DeviceDetailContent key={deviceId} deviceId={deviceId} /> : null;
};

const DeviceDetailContent: React.FC<{ deviceId: string }> = ({ deviceId }) => {
  const { version } = useApp();
  const [device, setDevice] = useState<Device | null>(null);
  const [tasks, setTasks] = useState<TaskItem[]>([]);
  const [evidence, setEvidence] = useState<any[]>([]);
  const [timeline, setTimeline] = useState<any[]>([]);
  const [error, setError] = useState<any>(null);
  const [dialog, setDialog] = useState<string | null>(null);
  const [binding, setBinding] = useState<BindingView | null>(null);
  const [editor, setEditor] = useState<BindingEditor | null>(null);
  const [saving, setSaving] = useState(false);
  const [requests] = useState(createLatestRequest);
  const [bindingRequests] = useState(createLatestRequest);
  const mounted = useRef(false);
  const operationPending = useRef(false);
  const bindingOptions = editor?.options;
  const bindingForm = editor?.draft.fields ?? {};
  const resourceSelections = editor?.draft.resources ?? {};
  const dirty = Boolean(editor && bindingDraftDirty(editor.draft));
  const bindingChanged = Boolean(editor && binding &&
    (editor.base.binding_revision !== binding.binding_revision || editor.base.config_version !== binding.config_version));

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      requests.invalidate();
      bindingRequests.invalidate();
    };
  }, [requests, bindingRequests]);

  const loadBinding = useCallback(async () => {
    if (!mounted.current) return;
    const current = bindingRequests.begin();
    try {
      const [view, options] = await Promise.all([
        callApi<BindingView>(`/v1/devices/${encodeURIComponent(deviceId)}/binding`),
        callApi<BindingOptions>(`/v1/devices/${encodeURIComponent(deviceId)}/binding/options`),
      ]);
      if (!current()) return;
      const selected = (items: BindingResourceOption[] = []) =>
        items.find((item) => item.selected)?.resource_id ?? "";
      const incoming = {
        base: view, options,
        draft: newBindingDraft(
          Object.fromEntries(BINDING_FIELDS.map(({ key }) => [key, String(view.binding[key] ?? "")])),
          { camera_binding_id: selected(options.camera_options), daemon_binding_id: selected(options.daemon_options),
            serial_binding_id: selected(options.serial_options) },
        ),
      };
      setBinding(view);
      setEditor((previous) => refreshBindingEditor(previous, incoming));
    } catch (e: any) {
      if (current()) setError(e);
    }
  }, [deviceId, bindingRequests]);

  useEffect(() => { void loadBinding(); }, [loadBinding, version]);

  const load = useCallback(async () => {
    if (!mounted.current) return;
    const current = requests.begin();
    try {
      const [nextDevice, taskData, tl] = await Promise.all([
        callApi<Device>(`/v1/devices/${encodeURIComponent(deviceId)}`),
        callApi<{ tasks: TaskItem[] }>(`/v1/tasks?device_id=${encodeURIComponent(deviceId)}`),
        callApi(`/v1/devices/${encodeURIComponent(deviceId)}/timeline`),
      ]);
      if (!current()) return;
      setDevice(nextDevice);
      setTasks(taskData.tasks);
      setTimeline(tl.timeline);
      setEvidence([]);
      const details = await Promise.all(taskData.tasks.slice(0, 5).map((task) =>
        callApi(`/v1/tasks/${encodeURIComponent(task.task_id)}`)));
      if (current()) setEvidence(details.flatMap((detail) => detail.evidence ?? []));
    } catch (e: any) {
      if (current()) setError(e);
    }
  }, [deviceId, requests]);

  useEffect(() => { void load(); }, [load, version]);

  const editBinding = (kind: "fields" | "resources", key: string, value: string) => {
    if (operationPending.current) return;
    setEditor((previous) => previous && {
      ...previous, draft: { ...previous.draft, [kind]: { ...previous.draft[kind], [key]: value } },
    });
  };

  const reloadBinding = () => {
    if (operationPending.current) return;
    setEditor(null);
    void loadBinding();
  };

  const runAction = async (action: string, reason: string) => {
    if (!mounted.current || operationPending.current) return;
    operationPending.current = true;
    const dangerous = action === "quarantine" || action === "unquarantine" || action === "reset-home";
    try {
      await callApi(`/v1/devices/${deviceId}/${action}`, { method: "POST", body: opContext(reason, { dangerous }) });
      if (!mounted.current) return;
      setDialog(null);
      void load();
    } catch (e: any) {
      if (mounted.current) setError(e);
    } finally {
      operationPending.current = false;
    }
  };

  const saveBinding = async (reason: string) => {
    if (!editor || !binding || !mounted.current || operationPending.current) return;
    if (bindingChanged) {
      setError({ title: "绑定版本已变化", suggestion: "请重新载入绑定后再保存。当前编辑未提交。" });
      return;
    }
    const updates: Record<string, any> = {};
    for (const { key, readOnly } of BINDING_FIELDS) {
      if (readOnly) continue;
      const original = editor.draft.originalFields[key] ?? "";
      const edited = (bindingForm[key] ?? "").trim();
      if (edited !== original) {
        updates[key] = key === "arm_slot" ? (edited === "" ? null : Number(edited)) : (edited === "" ? null : edited);
      }
    }
    for (const key of ["camera_binding_id", "daemon_binding_id", "serial_binding_id"]) {
      const selected = resourceSelections[key];
      if (selected && selected !== editor.draft.originalResources[key]) updates[key] = selected;
    }
    if (Object.keys(updates).length === 0) {
      setDialog(null);
      alert("绑定字段没有变化，未提交。");
      return;
    }
    operationPending.current = true;
    setSaving(true);
    try {
      const latest = await callApi<BindingView>(`/v1/devices/${deviceId}/binding`);
      if (!mounted.current) return;
      if (latest.binding_revision !== editor.base.binding_revision || latest.config_version !== editor.base.config_version) {
        setBinding(latest);
        setError({ title: "绑定版本已变化", suggestion: "请重新载入绑定后再保存。当前编辑未提交。" });
        return;
      }
      await callApi(`/v1/devices/${deviceId}/binding`, {
        method: "PUT",
        body: { ...opContext(reason, { dangerous: true }), ...updates },
      });
      if (!mounted.current) return;
      bindingRequests.invalidate();
      setEditor(null);
      setDialog(null);
      await Promise.all([load(), loadBinding()]);
    } catch (e: any) {
      if (mounted.current) setError(e);
    } finally {
      operationPending.current = false;
      if (mounted.current) setSaving(false);
    }
  };

  const runBindingToggle = async (action: "disable" | "enable", reason: string) => {
    if (!mounted.current || operationPending.current) return;
    operationPending.current = true;
    setSaving(true);
    try {
      await callApi(`/v1/devices/${deviceId}/binding/${action}`, {
        method: "POST",
        body: opContext(reason),
      });
      if (!mounted.current) return;
      setDialog(null);
      await Promise.all([load(), loadBinding()]);
    } catch (e: any) {
      if (mounted.current) setError(e);
    } finally {
      operationPending.current = false;
      if (mounted.current) setSaving(false);
    }
  };

  if (!device) return <div className="page"><ErrorBanner error={error} onClose={() => setError(null)} />加载中…</div>;

  return (
    <div className="page">
      <div className="page-head">
        <h2>{device.name_display} 详情</h2>
        <StatusTag display={device.status_display} />
      </div>
      <ErrorBanner error={error} onClose={() => setError(null)} />
      <div className="detail-columns">
        <section className="panel">
          <h3>状态</h3>
          <div>机械臂摄像头：{device.camera_id}（{device.camera_quality_display}）</div>
          <div>当前界面：{device.current_ui_state_display}</div>
          <div>微信账号：{device.wechat_account_id || "待确认"}</div>
          <DeviceConnection device={device} />
          {device.status_reason && <div>状态原因：{device.status_reason}</div>}
          <PrepareRun key={device.device_id} deviceId={device.device_id} onPrepared={load} />
          <div>归位状态：{device.home_ok ? "已归位" : "未确认归位"}</div>
          <div>租约：{device.lease_id ?? "无"}</div>
          <div>绑定说明：{device.binding_note}</div>
          {device.camera_source_display && (
            <div>帧来源：{device.camera_source_display}（最后帧 {device.camera_last_frame_at ?? "—"}
              {device.camera_frame_age_ms != null ? `，年龄 ${device.camera_frame_age_ms}ms` : ""}）</div>
          )}
          <div className="card-actions">
            <button onClick={() => setDialog("pause")} disabled={!canDeviceAction(device.status, "pause", device.home_ok)}>暂停</button>
            <button onClick={() => setDialog("resume")} disabled={!canDeviceAction(device.status, "resume", device.home_ok)}>恢复</button>
            <button onClick={() => setDialog("quarantine")} disabled={!canDeviceAction(device.status, "quarantine", device.home_ok)}>隔离</button>
            <button onClick={() => setDialog("unquarantine")} disabled={!canDeviceAction(device.status, "unquarantine", device.home_ok)}>解除隔离</button>
            <button onClick={() => setDialog("home-check")}>归位检查</button>
            <button onClick={() => setDialog("status-query")}>请求状态查询</button>
            <DebugLink deviceId={device.device_id} />
          </div>
        </section>
        <section className="panel">
          <h3>设备绑定（设备菜单）</h3>
          {binding && editor ? (
            <div>
              <div>设备编号（只读）：{device.device_id}</div>
              <div>绑定版本：第 {binding.binding_revision} 版 · 配置版本 {binding.config_version} · {binding.binding_enabled_display}</div>
              <div>daemon 会话：{binding.daemon_session ?? "—"} · 校准：{binding.calibration_status_display ?? "—"}</div>
              {bindingChanged && <div role="alert">绑定已在其他位置变更，当前编辑尚未提交。</div>}
              <div className="binding-form">
                <label>
                  摄像头资源（由服务端绑定表提供）
                  <select
                    value={resourceSelections.camera_binding_id ?? ""}
                    disabled={saving}
                    onChange={(event) => editBinding("resources", "camera_binding_id", event.target.value)}
                  >
                    <option value="">不更换摄像头</option>
                    {(bindingOptions?.camera_options ?? []).map((option) => (
                      <option key={option.resource_id} value={option.resource_id}>{option.label}</option>
                    ))}
                  </select>
                </label>
                <label>
                  daemon 资源（由服务端绑定表提供）
                  <select
                    value={resourceSelections.daemon_binding_id ?? ""}
                    disabled={saving}
                    onChange={(event) => editBinding("resources", "daemon_binding_id", event.target.value)}
                  >
                    <option value="">不更换 daemon</option>
                    {(bindingOptions?.daemon_options ?? []).map((option) => (
                      <option key={option.resource_id} value={option.resource_id}>{option.label}</option>
                    ))}
                  </select>
                </label>
                <label>
                  串口资源（由服务端绑定表提供）
                  <select
                    value={resourceSelections.serial_binding_id ?? ""}
                    disabled={saving}
                    onChange={(event) => editBinding("resources", "serial_binding_id", event.target.value)}
                  >
                    <option value="">不更换串口</option>
                    {(bindingOptions?.serial_options ?? []).map((option) => (
                      <option key={option.resource_id} value={option.resource_id}>{option.label}</option>
                    ))}
                  </select>
                </label>
                {BINDING_FIELDS.map(({ key }) => (
                  <label key={key}>
                    {binding.field_descriptions[key] ?? key}
                    <input
                      value={bindingForm[key] ?? ""}
                      disabled={saving}
                      onChange={(event) => editBinding("fields", key, event.target.value)}
                      placeholder={String(binding.binding[key] ?? "未绑定")}
                      readOnly={BINDING_FIELDS.find((field) => field.key === key)?.readOnly}
                    />
                  </label>
                ))}
              </div>
              <div className="card-actions">
                <button className="primary" onClick={() => setDialog("binding-save")} disabled={saving || !dirty || bindingChanged}>保存绑定变更</button>
                {dirty && <button onClick={reloadBinding} disabled={saving}>放弃编辑并重新载入</button>}
                <button className="danger" onClick={() => setDialog("binding-disable")} disabled={saving || !binding.binding_enabled}>禁用绑定</button>
                <button onClick={() => setDialog("binding-enable")} disabled={saving || binding.binding_enabled}>恢复绑定</button>
              </div>
              <p className="muted">地址和串口只能从服务端已登记资源中选择，前端不直接填写；保存为危险操作，需二次确认。保存后设备自动暂停并进入待校准门禁，摄像头/输入法/微信账号/串口/daemon 不允许与其他设备重复。</p>
              {binding.recent_audit && binding.recent_audit.length > 0 && (
                <div className="muted">
                  最近审计：{binding.recent_audit[0].occurred_at} · {binding.recent_audit[0].action_display}
                  {binding.recent_audit[0].operator_id ? ` · ${binding.recent_audit[0].operator_id}` : ""}
                </div>
              )}
            </div>
          ) : (
            <p className="muted">绑定信息加载中…</p>
          )}
        </section>
        <CameraPreview key={`${device.device_id}:${device.camera_id}:${device.binding_revision ?? 0}`} device={device} />
      </div>
      <section className="panel">
        <h3>任务</h3>
        <table className="table">
          <thead>
            <tr><th>任务编号</th><th>任务类型</th><th>状态</th><th>创建时间</th><th>说明</th></tr>
          </thead>
          <tbody>
            {tasks.map((task) => (
              <tr key={task.task_id}>
                <td>{task.task_id}</td>
                <td>{task.task_type_display}</td>
                <td><StatusTag display={task.status_display} /></td>
                <td>{task.created_at}</td>
                <td>{task.uncertainty_reason ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      <section className="panel">
        <h3>最近证据（含哈希）</h3>
        {evidence.length === 0 && <p className="muted">暂无证据</p>}
        <ul>
          {evidence.slice(0, 10).map((e) => (
            <li key={e.evidence_id}>
              {e.label} · {e.captured_at} · 哈希 {String(e.sha256).slice(0, 16)}…
            </li>
          ))}
        </ul>
      </section>
      <section className="panel">
        <h3>设备时间线</h3>
        <ul className="timeline">
          {timeline.slice(0, 15).map((entry, index) => (
            <li key={index}>
              <span className="muted">{entry.at}</span> · {entry.kind_display} · {entry.action_display ?? entry.note ?? ""}
              {entry.status_display ? ` · ${entry.status_display}` : ""}
            </li>
          ))}
        </ul>
      </section>
      {dialog && dialog.startsWith("binding-") ? (
        <ReasonDialog
          title={`${dialog === "binding-save" ? "保存设备绑定变更" : dialog === "binding-disable" ? "禁用设备绑定" : "恢复设备绑定"}：${device.name_display}`}
          dangerous={dialog === "binding-save"}
          onSubmit={(reason) =>
            dialog === "binding-save"
              ? saveBinding(reason)
              : runBindingToggle(dialog === "binding-disable" ? "disable" : "enable", reason)
          }
          onCancel={() => setDialog(null)}
        />
      ) : dialog ? (
        <ReasonDialog
          title={`${{pause:"暂停设备",resume:"恢复设备",quarantine:"隔离设备",unquarantine:"解除隔离","home-check":"归位检查","status-query":"人工状态查询"}[dialog] ?? dialog}：${device.name_display}`}
          dangerous={dialog === "quarantine" || dialog === "unquarantine"}
          onSubmit={(reason) => runAction(dialog, reason)}
          onCancel={() => setDialog(null)}
        />
      ) : null}
    </div>
  );
};
