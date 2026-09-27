import { Component, type ErrorInfo, type ReactNode } from 'react'

/**
 * 错误边界：**任何一块渲染崩了，都不许让整页白屏。**
 *
 * ## 为什么需要它
 *
 * 实测踩过一次：后端跑的是旧版本，接口返回里少了一个字段，
 * 前端那个组件读 `undefined.length` 抛异常——**整个叙事一致性页白屏**。
 *
 * 而那块内容只是页面的一节。少一节，用户顶多看到「对照表没了」；
 * 整页白屏，演示就没法继续了。
 *
 * 「演示动线中途不卡、不白屏」是阶段二的验收标准之一。
 * 靠每个组件自己写防御是不够的——总有一处会漏，而漏的那处
 * 恰好会在演示当天出现。错误边界是这件事的**机械保证**。
 *
 * ## 它不替代防御式取值
 *
 * 边界只能兜住「抛出来的异常」。`undefined` 本身不抛，
 * 所以 `!rows || rows.length < 2` 这类判断该写还是要写——
 * 边界是最后一道，不是第一道。
 */
interface Props {
  children: ReactNode
  /** 出错时显示在哪一块。给个名字，好知道是哪一节坏了。 */
  label?: string
}

interface State {
  error: Error | null
}

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // 出错的组件名会出现在调试信息里——不记的话，
    // 页面上只有一句「出错了」，看不出是哪一块。
    console.error('[工作台] 渲染出错：', this.props.label ?? '(未命名)', error, info)
  }

  render(): ReactNode {
    const { error } = this.state
    if (!error) return this.props.children

    return (
      <div className="async-state async-error" role="alert">
        <div className="async-title">
          {this.props.label ? `${this.props.label} 渲染失败` : '这一块渲染失败'}
        </div>
        {/* 把技术细节露出来——藏起来的话，排查只能靠猜 */}
        <div className="async-detail">{error.message}</div>
        <button type="button" onClick={() => this.setState({ error: null })}>
          重试
        </button>
        <p className="hint">
          页面的其余部分不受影响。若反复出现，多半是前后端版本不一致
          （后端在跑旧代码），重启后端即可。
        </p>
      </div>
    )
  }
}
