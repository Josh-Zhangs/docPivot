import { useRef, useState, type ReactNode } from 'react'
import type { TaskStatus, UiTask } from '../api/client'

interface Props {
  tasks: UiTask[]
  selectedId: string | null
  onSelect: (taskId: string) => void
  onDelete: (taskId: string) => void
  /** 批量组重命名（name 为空串 = 清除自定义名） */
  onRenameBatch?: (batchId: string, name: string) => void
}

const STATUS_LABEL: Record<TaskStatus, string> = {
  queued: '排队中',
  preprocessing: '预处理中',
  recognizing: '识别中',
  done: '完成',
  failed: '失败',
}

const WEEK = ['日', '一', '二', '三', '四', '五', '六']

/** 日期分组的 key（本地时区的年月日） */
function dayKey(d: Date): string {
  return `${d.getFullYear()}-${d.getMonth() + 1}-${d.getDate()}`
}

/** 日期分组标签：今天 / 昨天 / M月D日 周X / 一周以前 */
function dayLabel(d: Date): string {
  const now = new Date()
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const that = new Date(d.getFullYear(), d.getMonth(), d.getDate())
  const diff = Math.round((today.getTime() - that.getTime()) / 86_400_000)
  if (diff <= 0) return '今天'
  if (diff === 1) return '昨天'
  if (diff < 7) return `${d.getMonth() + 1}月${d.getDate()}日 周${WEEK[d.getDay()]}`
  return '一周以前'
}

/** 完成任务显示的时分秒（HH:MM:SS） */
function fmtTime(iso?: string): string | null {
  if (!iso) return null
  const d = new Date(iso)
  if (isNaN(d.getTime())) return null
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

/**
 * 左侧任务列表
 * 结构（三层）：
 *   日期分组（今天/昨天/M月D日/一周以前，可折叠；"一周以前"默认收起）
 *   └ 批量组头「批量处理 N 张」（同一次多张上传的任务归组，可折叠）
 *     └ 任务行（完成任务显示 HH:MM:SS）
 */
export default function TaskList({ tasks, selectedId, onSelect, onDelete, onRenameBatch }: Props) {
  // 折叠状态覆盖表：key = "date:..." 或 "batch:..."；未覆盖时用调用方给的默认值
  const [overrides, setOverrides] = useState<Record<string, boolean>>({})
  const toggle = (key: string, def: boolean) =>
    setOverrides((prev) => ({ ...prev, [key]: !(prev[key] ?? def) }))
  const isOpen = (key: string, def: boolean) => overrides[key] ?? def

  /**
   * 批量组重命名的行内编辑态：编辑哪一组 + 输入框草稿。
   * why 用 ref 记录"是否已取消"：输入框失焦（blur）也会触发提交，
   * 按 Esc 时先置位再关闭，blur 就不会把取消误当保存。
   */
  const [editingBatch, setEditingBatch] = useState<{ batchId: string; value: string } | null>(null)
  const cancelRef = useRef(false)

  const startRename = (batchId: string, current: string) => {
    cancelRef.current = false
    setEditingBatch({ batchId, value: current })
  }

  const finishRename = (save: boolean) => {
    const cur = editingBatch
    if (!cur) return
    setEditingBatch(null)
    if (save) onRenameBatch?.(cur.batchId, cur.value.trim())
  }

  if (tasks.length === 0) {
    return <div className="task-empty">暂无任务</div>
  }

  // --- 第一层：按日期分段（列表本身按创建时间倒序，分段保持顺序） ---
  const sections: { key: string; label: string; tasks: UiTask[] }[] = []
  for (const t of tasks) {
    const d = t.createdAt ? new Date(t.createdAt) : new Date()
    const dd = isNaN(d.getTime()) ? new Date() : d
    const gkey = `date:${dayKey(dd)}`   // 分组 key 统一带前缀，比较与展示用同一个
    const last = sections[sections.length - 1]
    if (last && last.key === gkey) last.tasks.push(t)
    else sections.push({ key: gkey, label: dayLabel(dd), tasks: [t] })
  }

  /** 单个任务行（保持定义顺序：先声明 taskNode 再在批量组中复用） */
  const taskNode = (t: UiTask): ReactNode => (
    <li
      key={t.taskId}
      className={`task-item ${t.taskId === selectedId ? 'active' : ''}`}
      onClick={() => onSelect(t.taskId)}
    >
      <div className="task-name" title={t.filename}>
        {t.filename}
      </div>
      <div className="task-meta">
        {/* 完成态用清晰的对勾图标：绿色即"识别完成"，未选中的行也能一眼看出状态 */}
        {t.status === 'done' ? (
          <span className="task-done-mark" title="识别完成">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3"
              strokeLinecap="round" strokeLinejoin="round"><path d="M20 6L9 17l-5-5" /></svg>
          </span>
        ) : (
          <span className={`badge badge-${t.status}`}>{STATUS_LABEL[t.status]}</span>
        )}
        {/* 完成任务显示时分秒（问题3）；进行中任务不显示时间 */}
        {(t.status === 'done' || t.status === 'failed') && (
          <span className="task-time" title="创建时间">{fmtTime(t.createdAt)}</span>
        )}
        {t.durationMs ? <span className="task-time">{(t.durationMs / 1000).toFixed(1)}s</span> : null}
        <button
          className="task-delete"
          title="删除任务"
          onClick={(e) => {
            e.stopPropagation()
            onDelete(t.taskId)
          }}
        >
          ✕
        </button>
      </div>
    </li>
  )

  /**
   * 第二层：批量组——同一次多张上传（共享 batchId）的连续任务，
   * 在组首插入「批量处理 N 张」可折叠头（问题4）
   */
  const renderTasks = (arr: UiTask[]): ReactNode[] => {
    const nodes: ReactNode[] = []
    let i = 0
    while (i < arr.length) {
      const t = arr[i]
      if (t.batchId) {
        let j = i
        while (j < arr.length && arr[j].batchId === t.batchId) j++
        const run = arr.slice(i, j)
        if (run.length > 1) {
          const bkey = `batch:${t.batchId}`
          const bopen = isOpen(bkey, true)
          const size = t.batchSize ?? run.length
          const batchId = t.batchId
          const editingThis = editingBatch?.batchId === batchId
          // 容器化：组头 + 成员装进同一个虚线框，一眼看出这批任务的范围，
          // 展开时不会和前后的散任务混在一起
          nodes.push(
            <li key={bkey} className="batch-group-item">
              <div className="batch-group">
                {editingThis ? (
                  /* 重命名态：组头变成输入框（Enter 保存 / Esc 取消 / 失焦保存） */
                  <div className="batch-head">
                    <input
                      className="batch-rename-input"
                      value={editingBatch?.value ?? ''}
                      placeholder={`批量处理 ${size} 张`}
                      maxLength={60}
                      autoFocus
                      onFocus={(e) => e.currentTarget.select()}
                      onChange={(e) => setEditingBatch({ batchId, value: e.target.value })}
                      onBlur={() => finishRename(!cancelRef.current)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') finishRename(true)
                        if (e.key === 'Escape') {
                          cancelRef.current = true
                          finishRename(false)
                        }
                      }}
                    />
                  </div>
                ) : (
                  <div className="batch-head">
                    <button
                      type="button"
                      className={`batch-toggle ${bopen ? '' : 'closed'}`}
                      onClick={() => toggle(bkey, true)}
                    >
                      <svg className="chev" viewBox="0 0 10 6" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round"><path d="M1 1l4 4 4-4" /></svg>
                      <span className="batch-title" title={t.batchName || `批量处理 ${size} 张`}>
                        {t.batchName || `批量处理 ${size} 张`}
                      </span>
                      {/* 改过名才单独标张数：默认名里已含"N 张"，不重复 */}
                      {t.batchName && <span className="batch-count">{size} 张</span>}
                    </button>
                    <button
                      type="button"
                      className="batch-rename"
                      title="重命名这一批"
                      aria-label="重命名这一批"
                      onClick={() => startRename(batchId, t.batchName ?? '')}
                    >
                      {/* 铅笔图标（内联 SVG，避免引入图标库） */}
                      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
                        strokeLinecap="round" strokeLinejoin="round">
                        <path d="M4 20h4L19 9a2.8 2.8 0 0 0-4-4L4 16v4z" />
                        <path d="M13.5 6.5l4 4" />
                      </svg>
                    </button>
                  </div>
                )}
                {bopen && <ul className="batch-members">{run.map((tt) => taskNode(tt))}</ul>}
              </div>
            </li>,
          )
        } else {
          run.forEach((tt) => nodes.push(taskNode(tt)))
        }
        i = j
      } else {
        nodes.push(taskNode(t))
        i++
      }
    }
    return nodes
  }

  return (
    <div className="task-groups">
      {sections.map((sec) => {
        // "一周以前"默认收起，其余日期组默认展开
        const def = sec.label !== '一周以前'
        return (
          <div key={sec.key} className="task-group">
            <button type="button" className="group-head" onClick={() => toggle(sec.key, def)}>
              <svg className={`chev ${isOpen(sec.key, def) ? '' : 'closed'}`} viewBox="0 0 10 6" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round"><path d="M1 1l4 4 4-4" /></svg>
              <span>{sec.label}</span>
              <span className="group-count">{sec.tasks.length}</span>
            </button>
            {isOpen(sec.key, def) && <ul className="task-list">{renderTasks(sec.tasks)}</ul>}
          </div>
        )
      })}
    </div>
  )
}
