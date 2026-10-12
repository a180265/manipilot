export interface ApiErrorPayload {
  key: string;
  title: string;
  suggestion: string;
  trace_id?: string;
  detail?: string;
}

/** 统一 API 调用：错误返回中文标题与处理建议，不把错误渲染成成功。 */
export async function callApi<T = any>(
  path: string,
  options: { method?: string; body?: unknown } = {}
): Promise<T> {
  const init: RequestInit = {
    method: options.method ?? "GET",
    headers: { "Content-Type": "application/json" },
  };
  if (options.body !== undefined) init.body = JSON.stringify(options.body);
  const response = await fetch(path, init);
  const text = await response.text();
  const payload = text ? JSON.parse(text) : {};
  if (!response.ok) {
    const error: ApiErrorPayload = payload?.error ?? {
      key: "unknown_error",
      title: `请求失败（HTTP ${response.status}）`,
      suggestion: "请稍后重试或联系运维人员。",
    };
    throw error;
  }
  return payload as T;
}

let operatorState = { operatorId: "操作员甲", role: "operator" };

export function setOperator(operatorId: string, role: string) {
  operatorState = { operatorId, role };
}

export function currentOperator() {
  return operatorState;
}

function newId(prefix: string) {
  return `${prefix}-${Math.random().toString(36).slice(2, 10)}`;
}

/** 写操作上下文：操作者、操作原因、关联编号、幂等请求编号、二次确认令牌。 */
export function opContext(reason: string, opts: { dangerous?: boolean } = {}) {
  const context: Record<string, unknown> = {
    operator_id: operatorState.operatorId,
    role: operatorState.role,
    reason,
    correlation_id: newId("corr"),
    client_request_id: newId("req"),
  };
  if (opts.dangerous) context.confirmation_token = "确认执行";
  return context;
}
