/**
 * 识别档位定义（面向用户的概念层）
 *
 * 设计意图：界面只暴露"快速 / 高效"这种用户能理解的质量档位，
 * 不出现 qwen2.5vl:7b / qwen2.5vl:32b 这类技术名。
 * 具体模型名只在本文件内做映射，随请求传给后端。
 */

export interface QualityMode {
  /** 档位标识（存 localStorage、作为 select 的 value） */
  key: 'fast' | 'balanced'
  /** 界面展示名 */
  label: string
  /** 悬停提示：说明适用场景，而不是说模型参数 */
  tip: string
  /** 用模型名判断是否匹配本档位（Ollama 返回的完整名，如 qwen2.5vl:7b） */
  match: (modelName: string) => boolean
}

/** 档位表：顺序即下拉显示顺序，从快到稳 */
export const QUALITY_MODES: QualityMode[] = [
  {
    key: 'fast',
    label: '快速',
    tip: '速度优先，适合先批量粗读；单张约 4~9 秒',
    match: (n) => n.includes('7b'),
  },
  {
    key: 'balanced',
    label: '高效',
    tip: '识别更准、格式与分隔线还原更稳，减少返工；单张约 20~26 秒',
    match: (n) => n.includes('32b'),
  },
]

/** 按 key 取档位定义 */
export function findMode(key: string): QualityMode | undefined {
  return QUALITY_MODES.find((m) => m.key === key)
}

/**
 * 由模型名反查档位展示名（历史任务里存的是模型名，界面仍要显示中文档位）
 * 匹配不到时返回空串——调用方据此不渲染徽章，绝不把原始模型名露出去
 */
export function modeLabelForModel(modelName: string): string {
  if (!modelName) return ''
  return QUALITY_MODES.find((m) => m.match(modelName))?.label ?? ''
}
