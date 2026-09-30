import type { ReactNode } from 'react'

/**
 * 四态边界：加载 / 出错 / 空 / 有内容。
 *
 * **所有数据面板都要走它。** 这是「不白屏」这件事的机械保证——
 * 靠自觉的话，总有几处忘了写 loading，而现场演示时那个地方恰好是白的。
 *
 * 空态与出错态刻意分开：空是「查到了，就是没有」，错是「没查成」。
 * 混成一个的话，后端挂了会显示成「这家公司没有数据」，很难查。
 */

interface Props {
  loading: boolean
  error: string | null
  /** 数据拿到了但是空的。与 error 不同：那是「没查成」，这是「查到了没有」。 */
  empty?: boolean
  emptyText?: string
  onRetry?: () => void
  children: ReactNode
}

export default function AsyncBoundary({
  loading,
  error,
  empty = false,
  emptyText = '没有数据。',
  onRetry,
  children,
}: Props) {
  if (loading) {
    return (
      <div className="async-state async-loading">
        <span className="spinner" aria-hidden="true" />
        正在加载…
      </div>
    )
  }

  if (error) {
    return (
      <div className="async-state async-error" role="alert">
        <div className="async-title">加载失败</div>
        {/* 直接展示后端写的中文说明——那是唯一有用的信息 */}
        <div className="async-detail">{error}</div>
        {onRetry && (
          <button type="button" onClick={onRetry}>
            重试
          </button>
        )}
      </div>
    )
  }

  if (empty) {
    return (
      <div className="async-state async-empty">
        <div className="async-detail">{emptyText}</div>
      </div>
    )
  }

  return <>{children}</>
}
