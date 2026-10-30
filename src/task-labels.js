// 任务键到中文名的唯一映射。
//
// 状态页（Host 侧 ui-routes.js）和状态卡片（客户端 client.js）都要用它，
// 放在这里共享，避免两处各写一份、改标签时漏掉一边。
// B0x 只是内部任务键，界面上不直接展示。
export const TASK_LABELS = {
  B01: '版面与格式',
  B02: '文字与表达',
  B03: '章节结构',
  B04: '章节引用',
  B05: '章节内容',
}

/** 取任务键对应的中文名；未知键原样返回。 */
export function taskLabel(key) {
  return TASK_LABELS[key] || key
}
