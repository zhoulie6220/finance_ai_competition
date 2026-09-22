/**
 * ECharts 的薄封装。
 *
 * 只做三件事：初始化、`setOption`、随容器尺寸变化 `resize`。
 * **不做任何数据加工**——传进来的 option 已经是后端算好的数字直接铺开，
 * 图上不会出现任何一个前端算出来的值。
 *
 * 用 `echarts/core` 按需引入而不是 `import * as echarts from 'echarts'`：
 * 后者会把全部图表类型（地图、桑基图、雷达图……）打进包里，
 * 而本系统只用得上折线、柱状两种。
 */

import { useEffect, useRef } from 'react'
import * as echarts from 'echarts/core'
import { BarChart, LineChart } from 'echarts/charts'
import {
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  TooltipComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import type { EChartsCoreOption } from 'echarts/core'

echarts.use([
  BarChart,
  LineChart,
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  TooltipComponent,
  CanvasRenderer,
])

interface Props {
  option: EChartsCoreOption
  height?: number
}

export function EChart({ option, height = 260 }: Props) {
  const ref = useRef<HTMLDivElement>(null)
  // 实例存在 ref 里而不是 state：放 state 会触发一次多余的渲染，
  // 而且首次 effect 拿到的还是 null。
  const chart = useRef<echarts.ECharts | null>(null)

  useEffect(() => {
    if (!ref.current) return
    chart.current = echarts.init(ref.current)

    // 容器宽度由 CSS 网格决定，窗口缩放时它跟着变，但 ECharts 不会自己知道。
    // 不接这个 observer 的话，拉宽窗口图就停在原来的宽度上，右边留一片空白。
    const ob = new ResizeObserver(() => chart.current?.resize())
    ob.observe(ref.current)

    return () => {
      ob.disconnect()
      chart.current?.dispose()
      chart.current = null
    }
  }, [])

  useEffect(() => {
    // notMerge=true：切换项目/指标时旧的系列要整个换掉。
    // 默认的合并模式会把新旧系列并排留下——图上会同时出现两家公司的曲线，
    // 而图例看起来完全正常。
    chart.current?.setOption(option, true)
  }, [option])

  return <div ref={ref} style={{ width: '100%', height }} />
}
