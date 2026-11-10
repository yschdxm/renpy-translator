// 文本选区检测：区分「单击编辑」与「拖选复制」
// 拖选后 mouseup 仍会触发 click——有非空选区时不应进入编辑
export function hasActiveTextSelection(): boolean {
  const selection = window.getSelection()
  return !!selection && !selection.isCollapsed && selection.toString().length > 0
}
