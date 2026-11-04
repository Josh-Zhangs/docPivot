import { useEffect, useState } from 'react'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkBreaks from 'remark-breaks'
import { copyText, type UiTask } from '../api/client'
import { modeLabelForModel } from '../lib/modes'

/** 语言代码 → 徽章文案（后端探测出的语言，ja/zh/en） */
const LANG_LABEL: Record<string, string> = { ja: '日本語', zh: '中文', en: 'English' }

interface Props {
  task: UiTask | null
  /** 重新识别（父组件负责建新任务并订阅流） */
  onRerun: (taskId: string) => Promise<void>
  /** 保存人工修正的 Markdown */
  onSaveEdit: (taskId: string, markdown: string) => Promise<void>
}

/**
 * 对照视图：左原图 / 右 Markdown 结果
 * 交互：重新识别按钮 + 双击结果进入人工修正编辑
 */
export default function CompareView({ task, onRerun, onSaveEdit }: Props) {
  const [copied, setCopied] = useState(false)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [rerunning, setRerunning] = useState(false)
  const [saving, setSaving] = useState(false)

  // 切换任务后重置所有临时状态
  useEffect(() => {
    setCopied(false)
    setEditing(false)
    setRerunning(false)
  }, [task?.taskId])

  if (!task) {
    return (
      <div className="compare-empty">
        {/* 空状态 = hero 首屏（借鉴视觉稿）：大标题只在无任务时出现，
            有任务后工作区永远留给"对照视图"，不浪费日常操作空间 */}
        <div className="hero-eyebrow">Documents, Understood</div>
        <h1 className="hero-title">
          让每一张单据，
          <br />
          都变成<b>可用的数据。</b>
        </h1>
        <p className="hero-sub">
          上传票据、表格或截图。docPivot
          会识别内容、整理结构，并自动检查关键数字——左侧选择「单张识别」或「批量识别」开始。
        </p>
        <div className="empty-feats">
          {/* 图标统一细线 SVG，不再使用 emoji */}
          <span className="feat-chip">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" /></svg>
            数据不出网
          </span>
          <span className="feat-chip">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z" /></svg>
            流式输出
          </span>
          <span className="feat-chip">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><path d="M14 2v6h6" /><path d="M8 13h8M8 17h5" /></svg>
            請求書 / 小票 / 截图
          </span>
          <span className="feat-chip">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M20 6L9 17l-5-5" /></svg>
            数字自检
          </span>
        </div>
      </div>
    )
  }

  const handleCopy = async () => {
    if (await copyText(task.markdown)) {
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    }
  }

  const handleDownload = () => {
    const blob = new Blob([task.markdown], { type: 'text/markdown;charset=utf-8' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = task.filename.replace(/\.[^.]+$/, '') + '.md'
    a.click()
    URL.revokeObjectURL(a.href)
  }

  /** 进入编辑态：以当前结果为草稿 */
  const startEdit = () => {
    setDraft(task.markdown)
    setEditing(true)
  }

  /** 保存人工修正 */
  const handleSave = async () => {
    setSaving(true)
    try {
      await onSaveEdit(task.taskId, draft)
      setEditing(false)
    } finally {
      setSaving(false)
    }
  }

  /** 重新识别 */
  const handleRerun = async () => {
    setRerunning(true)
    try {
      await onRerun(task.taskId)
    } finally {
      setRerunning(false)
    }
  }

  const streaming = task.status === 'recognizing'
  const canAct = task.status === 'done'
  // 本次识别所属档位（中文），取不到则不显示徽章
  const modeLabel = modeLabelForModel(task.model ?? '')

  return (
    <div className="compare">
      <div className="compare-pane">
        <div className="pane-header">
          <span>原图</span>
          <span className="pane-duration">{task.filename}</span>
        </div>
        {/* img-pane：点阵画布底，衬托白底票据 */}
        <div className="pane-body img-pane">
          <img src={task.previewUrl} alt={task.filename} />
        </div>
      </div>
      <div className="compare-pane">
        <div className="pane-header">
          <span>
            识别结果
            {task.durationMs ? (
              <span className="pane-duration">{(task.durationMs / 1000).toFixed(1)}s</span>
            ) : null}
            {/* 档位徽章：只显示"快速/高效"，不暴露具体模型名；老任务匹配不到就不显示 */}
            {modeLabel && (
              <span className="model-chip" title="本次识别使用的档位">
                {modeLabel}
              </span>
            )}
            {/* 语言徽章：识别前自动判定，决定加载哪套提示词（日语/中文） */}
            {task.lang && (
              <span className="lang-chip" title="自动判定的文档语言：按此加载对应提示词">
                {LANG_LABEL[task.lang] ?? task.lang}
              </span>
            )}
            {task.edited && <span className="edited-chip" title="结果经人工修正">✎ 已人工修正</span>}
          </span>
          <span className="pane-actions">
            {canAct && !editing && (
              <>
                <button className="btn-rerun" disabled={rerunning} onClick={handleRerun}
                  title="用原图再识别一次（历史保留，便于对比）">
                  {rerunning ? '识别中…' : '↻ 重新识别'}
                </button>
                <button onClick={startEdit} title="双击结果区域也可进入编辑">✎ 修改</button>
                <button className={copied ? 'btn-copy-done' : ''} onClick={handleCopy}>
                  {copied ? '✓ 已复制' : '复制'}
                </button>
                <button onClick={handleDownload} title="导出为 .md 文本文件，可用任意文本编辑器打开">
                  导出文件
                </button>
              </>
            )}
            {editing && (
              <>
                <button className="btn-copy-done" disabled={saving} onClick={handleSave}>
                  {saving ? '保存中…' : '✓ 保存'}
                </button>
                <button onClick={() => setEditing(false)}>取消</button>
              </>
            )}
          </span>
        </div>
        {/* 数字自检横幅：识别定稿/人工修正后产生；解析不出自检要素（checks_run=0）不显示。
            三态：可疑(有 error 级) / 提示(仅 warning，数字是自洽的) / 通过(无 issue)。 */}
        {!editing && task.validation && task.validation.checks_run > 0 && (() => {
          const issues = task.validation.issues ?? []
          const hasError = issues.some((it) => it.level === 'error')
          const state = hasError ? 'bad' : issues.length > 0 ? 'warn' : 'pass'
          return (
            <div className={`check-banner ${state}`}>
              <div className="check-head">
                {state === 'pass' && (
                  <>
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M20 6L9 17l-5-5" /></svg>
                    <b>数字自检通过</b>
                    <span className="check-count">{task.validation.checks_run} 项规则</span>
                  </>
                )}
                {state === 'warn' && (
                  <>
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="9" /><path d="M12 8v5M12 16h.01" /></svg>
                    <b>数字自检通过</b>
                    <span className="check-count">{task.validation.checks_run} 项规则 · 另有 {issues.length} 处提示待确认</span>
                  </>
                )}
                {state === 'bad' && (
                  <>
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 9v4M12 17h.01" /><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" /></svg>
                    <b>数字自检发现 {issues.filter((it) => it.level === 'error').length} 处可疑</b>
                    <span className="check-count">对照原图核对以下内容</span>
                  </>
                )}
              </div>
              {issues.length > 0 && (
                <ul className="check-issues">
                  {issues.map((it, i) => (
                    <li key={i}>
                      <span className={`check-level ${it.level}`}>{it.level === 'error' ? '错误' : '提示'}</span>
                      {it.message}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )
        })()}
        {editing ? (
          <div className="pane-body">
            <textarea
              className="edit-area"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              spellCheck={false}
              autoFocus
            />
            <div className="edit-hint">提示：直接改文字即可，单次换行会原样保留（与原图逐行对应）</div>
          </div>
        ) : (
          <div
            className={`pane-body markdown-body ${streaming && task.markdown ? 'streaming' : ''}`}
            onDoubleClick={canAct ? startEdit : undefined}
            title={canAct ? '双击进入人工修正' : undefined}
          >
            {task.status === 'failed' && <div className="result-error">识别失败：{task.error}</div>}
            {(task.status === 'done' || streaming) && task.markdown && (
              /* remark-breaks: 单个换行也渲染为 <br>——小票/单据逐行排版与原图逐行对齐 */
              <Markdown remarkPlugins={[remarkGfm, remarkBreaks]}>{task.markdown}</Markdown>
            )}
            {streaming && !task.markdown && (
              <div className="result-loading">正在调用本地视觉模型识别…</div>
            )}
            {task.status === 'preprocessing' && <div className="result-loading">图片预处理中…</div>}
            {task.status === 'queued' && <div className="result-loading">排队等待中…</div>}
          </div>
        )}
      </div>
    </div>
  )
}
