/** 行内编辑统一模式：点击单元格进入编辑，onBlur/Enter 提交。
 *  editingKey 守卫防止重复提交（Enter 后 blur 二次触发）；
 *  值未变化不发请求；save 异常向上抛，由调用方 toast。
 *  替代 TextsPage 的 editingId 守卫与 NamesPage 的 savedNames Map 两套写法。 */
import { ref } from 'vue'

export function useInlineEdit<Row>(
  rowKey: (row: Row) => string | number,
  getValue: (row: Row) => string,
  save: (row: Row, value: string) => Promise<void>,
) {
  /** 正在编辑的行 key；null 表示无编辑中 */
  const editingKey = ref<string | number | null>(null)
  /** 编辑中的文本（v-model 绑定） */
  const editingText = ref('')

  function isEditing(row: Row): boolean {
    return editingKey.value === rowKey(row)
  }

  function startEdit(row: Row): void {
    editingKey.value = rowKey(row)
    editingText.value = getValue(row)
  }

  /** 提交编辑：守卫重复触发；未修改直接退出；异常向上抛 */
  async function commitEdit(row: Row): Promise<void> {
    if (editingKey.value !== rowKey(row)) return // 守卫：Enter 后 blur 的重复触发
    editingKey.value = null
    if (editingText.value === getValue(row)) return // 未修改
    await save(row, editingText.value) // 异常向上抛，调用方 toast
  }

  function cancelEdit(): void {
    editingKey.value = null
  }

  return { editingKey, editingText, isEditing, startEdit, commitEdit, cancelEdit }
}
