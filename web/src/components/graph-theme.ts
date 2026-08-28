/** 图可视化统一主题：与 naive-ui darkTheme 对齐的 token
 *  配色取自 naive-ui 暗色主题：body #101014 / card #18181c /
 *  primary #63e2b7 / info #70c0e8 / warning #f2c97d / error #e88080 */

/** 统一配色（naive-ui darkTheme） */
export const GT = {
  bg: '#101014',
  card: '#1f1f24',
  border: 'rgba(255,255,255,0.14)',
  borderLight: 'rgba(255,255,255,0.07)',
  primary: '#63e2b7',   // naive 暗色主绿：入口/全译
  info: '#70c0e8',      // 蓝：menu 分支/选中
  warning: '#f2c97d',   // 黄：条件分支/部分译/结局
  error: '#e88080',     // 红：未决跳转
  muted: '#7a7a85',     // 灰：call 边/次级
  text: 'rgba(255,255,255,0.90)',
  text2: 'rgba(255,255,255,0.65)',
  text3: 'rgba(255,255,255,0.45)',
  track: 'rgba(255,255,255,0.08)',   // 进度条轨道/头像底
  radius: '8px',
  shadow: '0 1px 3px rgba(0,0,0,0.45)',
  shadowHover: '0 4px 14px rgba(0,0,0,0.6)',
}

/** 关系分类配色（暗色背景上的高辨识度版本） */
export const CATEGORY_META: Record<string, { color: string; label: string }> = {
  family: { color: '#f17c67', label: '亲属' },
  romantic: { color: '#f759ab', label: '恋爱' },
  friendly: { color: '#63e2b7', label: '友好' },
  hostile: { color: '#9254de', label: '敌对' },
  master_servant: { color: '#d3a637', label: '主从' },
  work: { color: '#70c0e8', label: '职业' },
  other: { color: '#8a8a95', label: '其他' },
  cooccur: { color: '#4c4c55', label: '共现' },
}

/** 阵营调色板（暗色适配） */
export const FACTION_PALETTE = [
  '#70c0e8', '#63e2b7', '#f2c97d', '#f759ab', '#9254de', '#2ab0a5',
  '#e88080', '#a0845e', '#7f8c9b', '#c687e0', '#4fc3f7', '#d4b445',
]

export interface SceneSpeaker { variable: string; name: string; hasAvatar: boolean }
