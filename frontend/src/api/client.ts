/**
 * 后端 API 封装
 * -----------
 * 统一使用相对路径（dev 模式由 Vite 代理到 :8000）
 * SSE 事件流约定（与后端 routers/ocr.py 对应）：
 *   snapshot  —— 订阅时的一次性快照（status/markdown/error）
 *   progress  —— 状态变更（preprocessing/recognizing）
 *   chunk     —— Markdown 增量片段
 *   done      —— 识别完成（含完整 markdown）
 *   error     —— 识别失败
 */

export type TaskStatus =
  | 'queued'
  | 'preprocessing'
  | 'recognizing'
  | 'done'
  | 'failed'

/** 单条数字自检问题 */
export interface ValidationIssue {
  rule: string
  level: 'error' | 'warning'
  message: string
}

/** 数字自检结果（后端纯规则计算；null=无法解析出自检要素，不展示） */
export interface ValidationResult {
  ok: boolean
  checks_run: number
  issues: ValidationIssue[]
}

export interface UiTask {
  taskId: string
  filename: string
  /** 本地预览 URL（刚上传时是 objectURL，回看历史时是 /api/files/{id}） */
  previewUrl: string
  status: TaskStatus
  markdown: string
  error: string
  durationMs?: number
  /** 结果经人工修正过 */
  edited?: boolean
  /** 本次识别使用的模型名（用于双模型对比展示） */
  model?: string
  /** 数字自检结果（识别完成/人工修正后产生） */
  validation?: ValidationResult | null
  /** 创建时间（后端 ISO 字符串；本次会话新建的任务由前端生成）——用于日期分组与时分秒显示 */
  createdAt?: string
  /** 批量上传组 id（同一次多张上传的任务共享）；单张上传没有 */
  batchId?: string
  /** 该批量组的总张数（显示"批量处理 N 张"用） */
  batchSize?: number
  /** 批量组的自定义名称（用户重命名后才有；为空则显示"批量处理 N 张"） */
  batchName?: string
  /** 文档语言（ja/zh/en）：识别前自动探测，决定加载哪套提示词 */
  lang?: string
}

/** 拉取远端 Ollama 已安装的模型名列表（引擎不可达时返回空数组） */
export async function fetchModels(): Promise<string[]> {
  try {
    const res = await fetch('/api/models')
    if (!res.ok) return []
    const d = await res.json()
    return Array.isArray(d.models) ? (d.models as string[]) : []
  } catch {
    return []
  }
}

/** 上传图片，返回任务 id 列表（与文件顺序一一对应）；model 为空则用后端默认模型。
 *  batchId/batchSize：一次多张上传的批量标记，随任务落库，刷新后分组不丢。 */
export async function uploadImages(
  files: File[],
  model?: string,
  batchId?: string,
  batchSize?: number,
): Promise<{ taskId: string; filename: string }[]> {
  const fd = new FormData()
  files.forEach((f) => fd.append('files', f))
  // model/batch 走查询参数，与后端 create_ocr 的可选参数对应
  const params = new URLSearchParams()
  if (model) params.set('model', model)
  if (batchId) params.set('batch_id', batchId)
  if (batchSize) params.set('batch_size', String(batchSize))
  const qs = params.toString() ? `?${params.toString()}` : ''
  const res = await fetch(`/api/ocr${qs}`, { method: 'POST', body: fd })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.detail ?? `上传失败 (${res.status})`)
  }
  const data = await res.json()
  // 后端返回 snake_case（task_id），这里统一转成前端驼峰命名
  return data.tasks.map((t: { task_id: string; filename: string }) => ({
    taskId: t.task_id,
    filename: t.filename,
  }))
}

/**
 * 订阅某任务的 SSE 流。
 * 返回关闭函数（组件卸载时调用，避免泄漏 EventSource）。
 */
export function subscribeTask(
  taskId: string,
  handlers: {
    onSnapshot?: (
      status: TaskStatus,
      markdown: string,
      error: string,
      validation?: ValidationResult | null,
      lang?: string,
    ) => void
    onProgress?: (status: TaskStatus, lang?: string) => void
    onChunk?: (chunk: string) => void
    onDone?: (
      markdown: string,
      durationMs: number,
      validation?: ValidationResult | null,
      lang?: string,
    ) => void
    onError?: (error: string) => void
  },
): () => void {
  const es = new EventSource(`/api/ocr/${taskId}/stream`)

  const parse = <T,>(ev: MessageEvent): T => JSON.parse(ev.data)

  es.addEventListener('snapshot', (ev) => {
    const d = parse<{ status: TaskStatus; markdown: string; error: string; validation?: ValidationResult | null; lang?: string }>(ev)
    handlers.onSnapshot?.(d.status, d.markdown ?? '', d.error ?? '', d.validation ?? null, d.lang || undefined)
    if (d.status === 'done' || d.status === 'failed') es.close()
  })
  es.addEventListener('progress', (ev) => {
    const d = parse<{ status: TaskStatus; lang?: string }>(ev)
    handlers.onProgress?.(d.status, d.lang || undefined)
  })
  es.addEventListener('chunk', (ev) => {
    const d = parse<{ chunk: string }>(ev)
    handlers.onChunk?.(d.chunk)
  })
  es.addEventListener('done', (ev) => {
    const d = parse<{ markdown: string; duration_ms: number; validation?: ValidationResult | null; lang?: string }>(ev)
    handlers.onDone?.(d.markdown, d.duration_ms, d.validation ?? null, d.lang || undefined)
    es.close()
  })
  es.addEventListener('error', (ev) => {
    // 区分：服务端业务 error 事件（MessageEvent）vs 连接层 error（Event）
    const me = ev as MessageEvent
    if (me.data) {
      const d = parse<{ error: string }>(me)
      handlers.onError?.(d.error)
    }
    es.close()
  })
  es.onerror = () => {
    // 连接层错误（后端重启/网络断开）——交由界面显示，可重新上传或刷新
    es.close()
  }

  return () => es.close()
}

/** 拉取历史任务列表 */
export async function fetchTasks(): Promise<UiTask[]> {
  const res = await fetch('/api/tasks')
  if (!res.ok) throw new Error('获取任务列表失败')
  const data = await res.json()
  return data.tasks.map(
    (t: Record<string, string | number>): UiTask => ({
      taskId: String(t.id),
      filename: String(t.filename),
      previewUrl: `/api/files/${t.id}`,
      status: t.status as TaskStatus,
      markdown: String(t.markdown ?? ''),
      error: String(t.error ?? ''),
      durationMs: Number(t.duration_ms ?? 0),
      edited: Boolean(t.edited),
      model: t.model ? String(t.model) : undefined,
      validation: (t.validation as unknown as ValidationResult | null) ?? null,
      createdAt: t.created_at ? String(t.created_at) : undefined,
      batchId: t.batch_id ? String(t.batch_id) : undefined,
      batchSize: t.batch_size ? Number(t.batch_size) : undefined,
      batchName: t.batch_name ? String(t.batch_name) : undefined,
      lang: t.lang ? String(t.lang) : undefined,
    }),
  )
}

/** 删除任务 */
export async function deleteTask(taskId: string): Promise<void> {
  const res = await fetch(`/api/tasks/${taskId}`, { method: 'DELETE' })
  if (!res.ok) throw new Error('删除失败')
}

/**
 * 批量组重命名。name 传空串 = 清除自定义名（回落为「批量处理 N 张」）。
 * 后端把同组所有任务的 batch_name 一起改掉，刷新后名字仍在。
 */
export async function renameBatch(batchId: string, name: string): Promise<void> {
  const res = await fetch(`/api/batches/${encodeURIComponent(batchId)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  })
  if (!res.ok) throw new Error('重命名失败')
}

/** 重新识别：用已存原图再跑一遍，返回新任务（旧任务保留可对比）；model 为空则用后端默认模型 */
export async function rerunTask(
  taskId: string,
  model?: string,
): Promise<{ taskId: string; filename: string }> {
  const qs = model ? `?model=${encodeURIComponent(model)}` : ''
  const res = await fetch(`/api/tasks/${taskId}/rerun${qs}`, { method: 'POST' })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.detail ?? `重新识别失败 (${res.status})`)
  }
  const d = await res.json()
  return { taskId: d.task_id, filename: d.filename }
}

/** 保存人工修正后的 Markdown；后端会重跑数字自检并返回最新结果 */
export async function saveMarkdown(taskId: string, markdown: string): Promise<ValidationResult | null> {
  const res = await fetch(`/api/tasks/${taskId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ markdown }),
  })
  if (!res.ok) throw new Error('保存失败')
  const d = await res.json()
  return d.validation ?? null
}

/** 复制文本到剪贴板（含降级方案：预览面板/iframe 中 Clipboard API 可能被禁用） */
export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    // 降级：隐藏 textarea + execCommand（兼容非安全上下文/受限 iframe）
    try {
      const ta = document.createElement('textarea')
      ta.value = text
      ta.style.position = 'fixed'
      ta.style.opacity = '0'
      document.body.appendChild(ta)
      ta.focus()
      ta.select()
      const ok = document.execCommand('copy')
      document.body.removeChild(ta)
      return ok
    } catch {
      return false
    }
  }
}
