import { useRef, useState } from 'react'

interface Props {
  onFiles: (files: File[]) => void
}

/**
 * 上传面板：双入口（单张识别 / 批量识别）+ 整块拖拽
 *
 * 行为约定（第三版教训之后定死）：
 * input 恒带 multiple（多选能力由浏览器原生保证），并且
 * 【选了几张就处理几张】——不管从哪个入口打开。
 * "单张/批量"只是打开对话框的两个按钮，真正的意图以用户选中的
 * 文件为准：选 1 张=单张，选 N 张=批量。此前"单张模式裁剪到
 * 1 张"的实现会静默丢掉用户多选的文件（实测 bug：选 2 张只传 1 张）。
 */
export default function UploadPanel({ onFiles }: Props) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [dragOver, setDragOver] = useState(false)

  /** 选中即全部处理（过滤非图片） */
  const pick = (list: FileList | null) => {
    if (!list || list.length === 0) return
    const imgs = Array.from(list).filter((f) => f.type.startsWith('image/'))
    if (imgs.length > 0) onFiles(imgs)
  }

  const openDialog = () => {
    inputRef.current?.click()
  }

  return (
    <div
      className={`upload-panel ${dragOver ? 'drag-over' : ''}`}
      onClick={openDialog}
      onDragOver={(e) => {
        e.preventDefault()
        setDragOver(true)
      }}
      onDragLeave={() => setDragOver(false)}
      onDrop={(e) => {
        e.preventDefault()
        setDragOver(false)
        pick(e.dataTransfer.files)
      }}
    >
      {/* 唯一的隐藏 input：恒带 multiple，选几张处理几张 */}
      <input
        ref={inputRef}
        type="file"
        accept="image/png,image/jpeg,image/webp,image/bmp"
        multiple
        hidden
        onChange={(e) => {
          pick(e.target.files)
          e.target.value = '' // 允许重复选择同一文件
        }}
      />

      <div className="upload-entries">
        {/* 入口一：单张识别（深墨主按钮）——习惯上一次处理一张的日常动线 */}
        <button
          type="button"
          className="upload-entry primary"
          onClick={(e) => {
            e.stopPropagation() // 不触发面板整体的点击
            openDialog()
          }}
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
            strokeLinecap="round" strokeLinejoin="round">
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
            <path d="M14 2v6h6" />
          </svg>
          单张识别
          <small>处理一张</small>
        </button>
        {/* 入口二：批量识别——一次一叠票的动线 */}
        <button
          type="button"
          className="upload-entry secondary"
          onClick={(e) => {
            e.stopPropagation()
            openDialog()
          }}
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
            strokeLinecap="round" strokeLinejoin="round">
            <path d="M4 7h12M4 12h16M4 17h12" />
            <rect x="17" y="3" width="4" height="4" rx="1" />
          </svg>
          批量识别
          <small>一次多张</small>
        </button>
      </div>
      <div className="upload-hint">选几张就处理几张 · JPG / PNG / WebP / BMP</div>
      <div className="upload-secure">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
          strokeLinecap="round" strokeLinejoin="round">
          <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
          <path d="m9 12 2 2 4-4" />
        </svg>
        文件在本机处理，数据不出网
      </div>
    </div>
  )
}
