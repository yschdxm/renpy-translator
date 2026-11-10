<script setup lang="ts">
/** 提示词设置：全部 AI 提示词可改（全局/项目覆盖），单条/全部恢复默认 */
import { computed, nextTick, onMounted, ref } from 'vue'
import {
  NAlert, NButton, NCard, NDivider, NEmpty, NInput, NPopconfirm, NScrollbar,
  NSelect, NSpace, NTag, NText, NTooltip, useMessage,
} from 'naive-ui'
import { RefreshOutline } from '@vicons/ionicons5'
import { api, toastError, toastOk } from '../api/client'
import { renderIcon } from '../components/icons'
import PageHelp from '../components/PageHelp.vue'
import { useSessionStore } from '../stores/session'
import { usePageScroll } from '../composables/usePageScroll'

const message = useMessage()
const session = useSessionStore()

interface PromptItem {
  id: string
  category: string
  name: string
  description: string
  default_text: string
  global_override: string | null
  project_override: string | null
  effective_text: string
  source: 'default' | 'global' | 'project'
  placeholders: { token: string; desc: string }[]
  contract_anchors: string[]
  cache_note: string
}

const prompts = ref<PromptItem[]>([])
const categories = ref<string[]>([])
const hasProject = ref(false)
const loading = ref(false)

const selectedId = ref('')
const scope = ref<'global' | 'project'>('global')
const editText = ref('')
const saving = ref(false)
const warnings = ref<{ kind: string; message: string }[]>([])

const selected = computed(() =>
  prompts.value.find((p) => p.id === selectedId.value) || null)

// 左栏按分类分组（保 REGISTRY 顺序）
const grouped = computed(() =>
  categories.value.map((c) => ({
    category: c,
    items: prompts.value.filter((p) => p.category === c),
  })).filter((g) => g.items.length))

async function load() {
  loading.value = true
  try {
    const data = await api.get<{
      prompts: PromptItem[]; categories: string[]; has_project: boolean
    }>('/api/prompts')
    prompts.value = data.prompts
    categories.value = data.categories
    hasProject.value = data.has_project
    if (!hasProject.value && scope.value === 'project') scope.value = 'global'
    if (!selectedId.value && data.prompts.length) select(data.prompts[0])
    else if (selectedId.value) refreshEdit()
  } catch (e) {
    toastError(message, e)
  } finally {
    loading.value = false
  }
}

function select(p: PromptItem) {
  selectedId.value = p.id
  warnings.value = []
  refreshEdit()
}

function refreshEdit() {
  const p = selected.value
  if (!p) return
  // 编辑框显示当前作用域层的文本；项目层无覆盖时从全局/默认继承为起点
  editText.value = scope.value === 'project'
    ? (p.project_override ?? p.global_override ?? p.default_text)
    : (p.global_override ?? p.default_text)
  warnings.value = []
}

async function save() {
  const p = selected.value
  if (!p || !editText.value.trim()) {
    toastError(message, new Error('提示词不能为空；要恢复默认请点「恢复默认」'))
    return
  }
  saving.value = true
  try {
    const data = await api.put<{ ok: boolean; warnings: typeof warnings.value }>(
      `/api/prompts/${encodeURIComponent(p.id)}`, {
        scope: scope.value, text: editText.value,
      })
    warnings.value = data.warnings || []
    toastOk(message, data.warnings?.length
      ? `已保存（${data.warnings.length} 条警告）` : '已保存')
    await load()
  } catch (e) {
    toastError(message, e)
  } finally {
    saving.value = false
  }
}

async function resetOne() {
  const p = selected.value
  if (!p) return
  try {
    await api.post(`/api/prompts/${encodeURIComponent(p.id)}/reset`, {})
    toastOk(message, `${p.name} 已恢复默认`)
    await load()
  } catch (e) {
    toastError(message, e)
  }
}

const resettingAll = ref(false)
async function resetAll() {
  resettingAll.value = true
  try {
    await api.post('/api/prompts/reset', {})
    toastOk(message, '全部提示词已恢复默认')
    await load()
  } catch (e) {
    toastError(message, e)
  } finally {
    resettingAll.value = false
  }
}

const scopeOptions = computed(() => [
  { label: '全局（所有项目）', value: 'global' },
  { label: `本项目${session.currentProject ? `（${session.currentProject}）` : ''}`,
    value: 'project', disabled: !hasProject.value },
])

// ---- 占位符按钮：点击插入到编辑框光标处 ----
const editorBox = ref<HTMLElement | null>(null)

function insertAtCursor(snippet: string) {
  const ta = editorBox.value?.querySelector('textarea')
  if (!ta) {
    editText.value += snippet
    return
  }
  const start = ta.selectionStart ?? editText.value.length
  const end = ta.selectionEnd ?? start
  editText.value =
    editText.value.slice(0, start) + snippet + editText.value.slice(end)
  nextTick(() => {
    ta.focus()
    const pos = start + snippet.length
    ta.setSelectionRange(pos, pos)
  })
}

const insertToken = (token: string) => insertAtCursor(`[[${token}]]`)
// 可选段落：[[?x]] 注入值 + 值空时整个自然段（含段内固定文字）消失
const insertOpt = (token: string) => insertAtCursor(`[[?${token}]]`)
const insertComment = () => insertAtCursor('# ')

const sourceTag = (p: PromptItem) => ({
  default: { text: '默认', type: 'default' as const },
  global: { text: '全局自定义', type: 'info' as const },
  project: { text: '项目覆盖', type: 'warning' as const },
})[p.source]

onMounted(load)
usePageScroll(load)
</script>

<template>
  <div>
    <n-space align="center" style="width: 100%; margin-bottom: 12px">
      <h2 style="margin: 0">提示词设置</h2>
      <page-help style="margin-left: auto" :section="'13'" />
      <n-popconfirm @positive-click="resetAll">
        <template #trigger>
          <n-button size="small" type="error" quaternary
                    :loading="resettingAll">全部恢复默认</n-button>
        </template>
        将清除全部自定义提示词（全局 + 项目覆盖），回到内置默认。确定？
      </n-popconfirm>
      <n-button size="small" quaternary
                :render-icon="renderIcon(RefreshOutline)" @click="load">刷新</n-button>
      <n-text depth="3" style="font-size: 12px">
        修改立即生效（下次调用生效）；带警告的锚点被删除可能导致解析失败
      </n-text>
    </n-space>

    <div style="display: flex; gap: 12px; align-items: flex-start">
      <!-- 左栏：分类清单 -->
      <n-card size="small" style="width: 260px; flex-shrink: 0">
        <n-scrollbar style="max-height: 75vh">
          <div v-for="g in grouped" :key="g.category" style="margin-bottom: 10px">
            <n-text depth="3" style="font-size: 12px; font-weight: 600">{{ g.category }}</n-text>
            <div v-for="p in g.items" :key="p.id"
                 :style="{
                   padding: '4px 8px', borderRadius: '4px', cursor: 'pointer',
                   background: p.id === selectedId ? 'rgba(24, 160, 88, 0.15)' : 'transparent',
                   marginTop: '2px',
                 }"
                 @click="select(p)">
              <n-text style="font-size: 13px">{{ p.name }}</n-text>
              <n-tag v-if="p.source !== 'default'" size="tiny" :type="sourceTag(p).type"
                     style="margin-left: 4px">{{ sourceTag(p).text }}</n-tag>
            </div>
          </div>
        </n-scrollbar>
      </n-card>

      <!-- 右栏：编辑器 -->
      <div style="flex: 1; min-width: 0">
        <n-card v-if="selected" size="small">
          <n-space align="center" justify="space-between">
            <div>
              <n-text strong>{{ selected.name }}</n-text>
              <n-text depth="3" style="font-size: 12px; margin-left: 8px">
                {{ selected.description }}
              </n-text>
            </div>
            <n-select v-model:value="scope" :options="scopeOptions"
                      size="small" style="width: 220px"
                      @update:value="refreshEdit" />
          </n-space>

          <div ref="editorBox">
            <n-input v-model:value="editText" type="textarea"
                     :rows="18" style="margin-top: 10px; font-family: monospace" />
          </div>

          <n-space style="margin-top: 10px">
            <n-button size="small" type="primary" :loading="saving" @click="save">
              保存{{ scope === 'project' ? '（本项目）' : '（全局）' }}
            </n-button>
            <n-popconfirm @positive-click="resetOne">
              <template #trigger>
                <n-button size="small">恢复默认</n-button>
              </template>
              清除该条的全局与项目覆盖，回到内置默认。确定？
            </n-popconfirm>
            <n-button size="small" quaternary @click="refreshEdit">撤销修改</n-button>
          </n-space>

          <!-- 保存警告（不阻断） -->
          <n-alert v-if="warnings.length" type="warning" title="保存警告"
                   style="margin-top: 10px">
            <div v-for="(w, i) in warnings" :key="i" style="font-size: 12px">
              · {{ w.message }}
            </div>
          </n-alert>

          <!-- 占位符 / 契约 / 缓存说明 -->
          <div style="margin-top: 14px">
            <n-text depth="3" style="font-size: 12px; font-weight: 600">
              占位符（点击插入到光标处）
            </n-text>
            <div style="margin-top: 4px">
              <n-space v-if="selected.placeholders.length" size="small">
                <n-tooltip v-for="ph in selected.placeholders" :key="ph.token"
                           trigger="hover">
                  <template #trigger>
                    <n-button size="tiny" quaternary @click="insertToken(ph.token)">
                      [[{{ ph.token }}]]
                    </n-button>
                  </template>
                  纯注入：{{ ph.desc }}——值为空就填空，周围文字照常发
                </n-tooltip>
                <n-divider vertical />
                <n-tooltip v-for="ph in selected.placeholders" :key="'o' + ph.token"
                           trigger="hover">
                  <template #trigger>
                    <n-button size="tiny" quaternary @click="insertOpt(ph.token)">
                      [[?{{ ph.token }}]]
                    </n-button>
                  </template>
                  可选段落：{{ ph.desc }}——值为空时整个自然段（空行分隔，
                  连同段内固定文字）连同一个空行一起消失；内容块标题随内容自动注入，
                  写成 # 注释即可
                </n-tooltip>
                <n-divider vertical />
                <n-tooltip trigger="hover">
                  <template #trigger>
                    <n-button size="tiny" quaternary @click="insertComment">
                      # 注释
                    </n-button>
                  </template>
                  插入注释行：只写给人看，发送给 AI 前自动剔除
                  （## 开头的是正文小标题，不会被剔除；
                  内容块标题随内容自动注入，模板里用注释标注即可）
                </n-tooltip>
              </n-space>
              <n-text v-else depth="3" style="font-size: 12px">（无占位符）</n-text>
            </div>
            <n-text depth="3" style="font-size: 12px">
              语法只有两种标签：[[x]] 纯注入；[[?x]] 可选段落（值空时整个自然段连同一个空行消失）。
              # 开头的行是注释，发送前自动剔除。内容块标题（如【作品风格指南】）随内容自动注入，
              模板里用注释标给人看即可，不要写成正文——写正文会双重出现。换行就是模板里的普通换行。</n-text>
          </div>

          <div v-if="selected.contract_anchors.length" style="margin-top: 10px">
            <n-text depth="3" style="font-size: 12px; font-weight: 600">解析契约锚点</n-text>
            <n-space size="small" style="margin-top: 4px">
              <n-tag v-for="a in selected.contract_anchors" :key="a"
                     size="small" type="warning">{{ a }}</n-tag>
            </n-space>
            <n-text depth="3" style="font-size: 12px">
              程序按这些锚点解析模型输出，删除会导致解析失败（保存时会警告）。
            </n-text>
          </div>

          <div v-if="selected.cache_note" style="margin-top: 10px">
            <n-text depth="3" style="font-size: 12px; font-weight: 600">缓存契约</n-text>
            <n-text depth="3" style="font-size: 12px; display: block">
              {{ selected.cache_note }}
            </n-text>
          </div>
        </n-card>
        <n-empty v-else description="加载中..." />
      </div>
    </div>
  </div>
</template>
