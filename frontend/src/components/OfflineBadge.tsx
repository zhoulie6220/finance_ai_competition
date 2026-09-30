import type { Health } from '../api/types'

/**
 * 离线回放模式横幅。
 *
 * **这不是装饰。** docs/00 把「离线回放时假装实时」列为**诚信问题**：
 * 现场断网时系统走的是预录响应，如果界面不标注，评审会以为那是模型现场
 * 分析出来的。所以只要 `llm_mode === 'replay'`，这个横幅就常驻，
 * 且不受任何折叠/隐藏逻辑影响。
 */
export default function OfflineBadge({ health }: { health: Health | null }) {
  if (!health) return null

  if (health.llm_mode === 'replay') {
    return (
      <div className="offline-banner" role="status">
        <strong>离线回放模式</strong>
        <span>当前展示的是预录的模型响应，不是实时调用。</span>
      </div>
    )
  }

  // 实时模式但没配密钥：模型相关的功能不可用。也要说，否则页面上
  // 一个「没有结果」的模型面板会被读成「模型没找到证据」。
  if (!health.llm_configured) {
    return (
      <div className="offline-banner offline-banner-warn" role="status">
        <strong>未配置模型密钥</strong>
        <span>涉及模型调用的功能不可用；财务事实、勾稽校验、证据链不受影响。</span>
      </div>
    )
  }

  return null
}
