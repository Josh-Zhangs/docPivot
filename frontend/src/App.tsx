import { useCallback, useEffect, useRef, useState } from 'react'
import {
  deleteTask,
  fetchTasks,
  renameBatch,
  rerunTask,
  saveMarkdown,
  subscribeTask,
  uploadImages,
  type UiTask,
} from './api/client'
import UploadPanel from './components/UploadPanel'
import TaskList from './components/TaskList'
import CompareView from './components/CompareView'
import { QUALITY_MODES, findMode, type QualityMode } from './lib/modes'

/**
 * 档位 → 实际模型名
 * 该档位在引擎上找不到对应模型时，退回第一个可用视觉模型，保证功能不中断
 */
function modelForMode(list: string[], key: string): string {
  const m = findMode(key)
  return (m && list.find((n) => m.match(n))) ?? list[0] ?? ''
}

export default function App() {
  const [tasks, setTasks] = useState<UiTask[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [globalError, setGlobalError] = useState('')
  const [engineOk, setEngineOk] = useState<boolean | null>(null) // null=检测中
  /** 引擎上可用的视觉模型（原始名，仅内部映射用，不直接显示） */
  const [models, setModels] = useState<string[]>([])
  /** 当前档位（界面只有"快速/高效"两个概念） */
  const [modeKey, setModeKey] = useState<QualityMode['key']>('fast')
  /** 当前档位对应的实际模型名；空串 = 走后端 .env 默认 */
  const [selectedModel, setSelectedModel] = useState('')
  // 任务 id -> SSE 关闭函数；组件卸载时统一断开
  const unsubRef = useRef(new Map<string, () => void>())

  /** 启动时拉取历史任务 + 检测远端引擎连通性 + 拉模型列表（顶栏状态芯片与选择器） */
  useEffect(() => {
    fetchTasks()
      .then((list) => {
        setTasks(list)
        const first = list.find((t) => t.status === 'done' || t.status === 'failed')
        if (first) setSelectedId(first.taskId)
      })
      .catch((e) => setGlobalError(String(e)))
    fetch('/api/models')
      .then((r) => r.json())
      .then((d) => {
        setEngineOk(Boolean(d.ok))
        const names: string[] = Array.isArray(d.models) ? d.models : []
        // 视觉模型（含 vl 的）才用于识图；纯文本模型（qwen3/mistral 等）不计入档位
        const visionModels = names.filter((n) => n.includes('vl'))
        setModels(visionModels)
        // 恢复上次档位；没选过或已失效则默认"快速"
        const savedKey = localStorage.getItem('docpivot.mode') ?? ''
        const valid = QUALITY_MODES.find((m) => m.key === savedKey)
        const key = valid?.key ?? 'fast'
        setModeKey(key)
        setSelectedModel(modelForMode(visionModels, key))
      })
      .catch(() => setEngineOk(false))
    // 卸载时断开所有 SSE
    return () => unsubRef.current.forEach((un) => un())
  }, [])

  /** 切换档位：同步更新实际模型名并持久化（界面不暴露模型名） */
  const handleModeChange = useCallback(
    (key: string) => {
      const k = (findMode(key)?.key ?? 'fast') as QualityMode['key']
      setModeKey(k)
      localStorage.setItem('docpivot.mode', k)
      setSelectedModel(modelForMode(models, k))
    },
    [models],
  )

  /** 当前引擎上真正可用的档位（缺哪个模型就不显示哪个选项） */
  const availableModes = QUALITY_MODES.filter((m) => models.some((n) => m.match(n)))

  /** 更新单个任务字段 */
  const patchTask = useCallback((taskId: string, patch: Partial<UiTask>) => {
    setTasks((prev) => prev.map((t) => (t.taskId === taskId ? { ...t, ...patch } : t)))
  }, [])

  /** 订阅任务的 SSE 流，把增量写入对应任务 */
  const watchTask = useCallback(
    (task: UiTask) => {
      // 避免重复订阅
      if (unsubRef.current.has(task.taskId)) return
      const un = subscribeTask(task.taskId, {
        onSnapshot: (status, markdown, error, validation, lang) =>
          patchTask(task.taskId, { status, markdown, error, validation, lang }),
        onProgress: (status, lang) => patchTask(task.taskId, { status, lang }),
        onChunk: (chunk) =>
          setTasks((prev) =>
            prev.map((t) => (t.taskId === task.taskId ? { ...t, markdown: t.markdown + chunk } : t)),
          ),
        onDone: (markdown, durationMs, validation, lang) =>
          patchTask(task.taskId, { status: 'done', markdown, durationMs, validation, lang }),
        onError: (error) => patchTask(task.taskId, { status: 'failed', error }),
      })
      unsubRef.current.set(task.taskId, un)
    },
    [patchTask],
  )

  /** 上传：逐张建任务并订阅 */
  const handleFiles = useCallback(
    async (files: File[]) => {
      setGlobalError('')
      try {
        // 批量标记：一次上传多张时共享同一个 batchId，随上传请求发给后端落库，
        // 刷新/换浏览器后任务列表重新拉取时分组仍在（此前只存前端内存，刷新即散）
        const batchId = files.length > 1 ? `b${Date.now()}` : undefined
        const created = await uploadImages(files, selectedModel || undefined, batchId, files.length)
        const now = new Date().toISOString()
        const newTasks: UiTask[] = created.map((c, i) => ({
          taskId: c.taskId,
          filename: c.filename,
          previewUrl: URL.createObjectURL(files[i]),
          status: 'queued',
          markdown: '',
          error: '',
          model: selectedModel || undefined,
          createdAt: now,
          batchId,
          batchSize: files.length,
        }))
        setTasks((prev) => [...newTasks, ...prev])
        setSelectedId(newTasks[0]?.taskId ?? null)
        newTasks.forEach(watchTask)
      } catch (e) {
        setGlobalError(e instanceof Error ? e.message : String(e))
      }
    },
    [watchTask, selectedModel],
  )

  const handleDelete = useCallback(
    async (taskId: string) => {
      unsubRef.current.get(taskId)?.()
      unsubRef.current.delete(taskId)
      await deleteTask(taskId).catch(() => undefined)
      setTasks((prev) => prev.filter((t) => t.taskId !== taskId))
      if (selectedId === taskId) setSelectedId(null)
    },
    [selectedId],
  )

  /** 重新识别：后端建新任务 → 前端加入列表、选中并订阅流 */
  const handleRerun = useCallback(
    async (taskId: string) => {
      const created = await rerunTask(taskId, selectedModel || undefined)
      // 新任务立即标注本次使用的模型，结果面板可直接对比
      const newTask: UiTask = {
        taskId: created.taskId,
        filename: created.filename,
        previewUrl: `/api/files/${created.taskId}`,
        status: 'queued',
        markdown: '',
        error: '',
        edited: false,
        model: selectedModel || undefined,
        createdAt: new Date().toISOString(),
      }
      setTasks((prev) => [newTask, ...prev])
      setSelectedId(created.taskId)
      watchTask(newTask)
    },
    [watchTask, selectedModel],
  )

  /** 保存人工修正结果到本地状态（后端已同步重跑数字自检） */
  const handleSaveEdit = useCallback(
    async (taskId: string, markdown: string) => {
      const validation = await saveMarkdown(taskId, markdown)
      patchTask(taskId, { markdown, edited: true, validation })
    },
    [patchTask],
  )

  /** 批量组重命名：先本地乐观更新（名字瞬间生效），失败再回滚并报错 */
  const handleRenameBatch = useCallback(async (batchId: string, name: string) => {
    let snapshot: UiTask[] = []
    setTasks((prev) => {
      snapshot = prev
      return prev.map((t) => (t.batchId === batchId ? { ...t, batchName: name || undefined } : t))
    })
    try {
      await renameBatch(batchId, name)
    } catch (e) {
      setTasks(snapshot)
      setGlobalError(e instanceof Error ? e.message : String(e))
    }
  }, [])

  const selected = tasks.find((t) => t.taskId === selectedId) ?? null

  return (
    <div className="app">
      {/* 背景光晕与噪点纹理由 body::before/::after 绘制，无需 DOM 节点 */}

      <header className="app-header">
        <div className="logo">
          <div className="logo-badge">
            {/* 深墨徽标 + 青柠 "d." 字标：视觉稿同款识别符号 */}
            <span className="logo-mark">d.</span>
          </div>
          <div>
            <div className="app-title">docPivot</div>
            {/* 品牌语：英文短语维持极简调性（中文 tagline 评审后撤掉） */}
            <div className="app-subtitle">
              <span className="brand-en">DOCUMENT INTELLIGENCE</span>
            </div>
          </div>
        </div>
        <span className="header-spacer" />
        {/* 识别档位选择器：界面只说"快速/高效"，具体模型名不露出 */}
        {availableModes.length > 0 && (
          <label className="model-select" title="识别档位：快速=速度优先，高效=格式还原更稳">
            <span className="model-label">档位</span>
            <select value={modeKey} onChange={(e) => handleModeChange(e.target.value)}>
              {availableModes.map((m) => (
                <option key={m.key} value={m.key} title={m.tip}>
                  {m.label}
                </option>
              ))}
            </select>
          </label>
        )}
        {/* 引擎状态芯片：绿点=Mac Ollama 可达；窄屏只留点（文字由 CSS 收起） */}
        <div className="engine-chip" title="日本 Mac 上的 Ollama 识别引擎">
          <span className={`engine-dot ${engineOk === null ? '' : engineOk ? 'on' : 'off'}`} />
          <span className="engine-text">
            {engineOk === null ? '检测引擎中…' : engineOk ? <>识别引擎 <b>已连接</b></> : <>识别引擎 <b>未连接</b></>}
          </span>
        </div>
        {globalError && <span className="global-error">{globalError}</span>}
      </header>

      <div className="app-body">
        <aside className="sidebar">
          <UploadPanel onFiles={handleFiles} />
          {/* 分组标题：给任务列表一个"目录"入口，数量实时 */}
          <div className="side-title">任务 · {tasks.length}</div>
          <TaskList
            tasks={tasks}
            selectedId={selectedId}
            onSelect={setSelectedId}
            onDelete={handleDelete}
            onRenameBatch={handleRenameBatch}
          />
        </aside>
        <main className="main">
          <CompareView task={selected} onRerun={handleRerun} onSaveEdit={handleSaveEdit} />
        </main>
      </div>
    </div>
  )
}
