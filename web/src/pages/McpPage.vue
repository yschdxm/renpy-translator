<script setup lang="ts">
/** MCP 接入页：让 AI 编程助手（Claude Code 等）经 Streamable HTTP 操作本工具 */
import { onMounted, ref } from 'vue'
import {
  NButton, NCard, NInput, NSelect, NSpace, NSwitch, NTag, NText, useMessage,
} from 'naive-ui'
import { CopyOutline, RefreshOutline } from '@vicons/ionicons5'
import { api, toastError, toastOk } from '../api/client'
import { renderIcon } from '../components/icons'
import PageHeader from '../components/PageHeader.vue'
import { usePageScroll } from '../composables/usePageScroll'

const message = useMessage()

interface McpSettings {
  enabled: boolean; tool_mode: string; has_token: boolean; endpoint: string
}
const mcp = ref<McpSettings>({
  enabled: false, tool_mode: 'curated', has_token: false, endpoint: '',
})
const mcpTokenPlain = ref('')   // 仅生成后展示一次
const mcpBusy = ref(false)

async function refresh() {
  mcp.value = await api.get<McpSettings>('/api/settings/mcp')
}

async function setMcpEnabled(v: boolean) {
  try {
    mcp.value = await api.put<McpSettings>('/api/settings/mcp', { enabled: v })
    toastOk(message, v ? 'MCP 已启用' : 'MCP 已关闭')
  } catch (e) {
    toastError(message, e)
    await refresh()
  }
}

async function setMcpMode(v: string) {
  try {
    mcp.value = await api.put<McpSettings>('/api/settings/mcp', { tool_mode: v })
    toastOk(message, v === 'full'
      ? '已切到全量工具：客户端需重新连接才能看到新列表'
      : '已切回精选工具：客户端需重新连接才能看到新列表')
  } catch (e) {
    toastError(message, e)
    await refresh()
  }
}

async function genMcpToken() {
  mcpBusy.value = true
  try {
    const data = await api.post<McpSettings & { token: string }>(
      '/api/settings/mcp/token')
    mcpTokenPlain.value = data.token
    mcp.value = { ...mcp.value, has_token: true }
    toastOk(message, 'Token 已生成（仅显示这一次，请立即复制保存）')
  } catch (e) {
    toastError(message, e)
  } finally {
    mcpBusy.value = false
  }
}

async function clearMcpToken() {
  try {
    mcp.value = await api.del<McpSettings>('/api/settings/mcp/token')
    mcpTokenPlain.value = ''
    toastOk(message, 'Token 已清除（当前无 token，仅本机可直连）')
  } catch (e) {
    toastError(message, e)
  }
}

async function copyText(text: string) {
  try {
    await navigator.clipboard.writeText(text)
    toastOk(message, '已复制')
  } catch {
    message.warning('剪贴板不可用，请手动复制')
  }
}

onMounted(refresh)
usePageScroll(refresh)
</script>

<template>
  <div style="max-width: 860px">
    <page-header title="MCP 接入" help-section="16">
      <template #actions>
        <n-button size="small" :render-icon="renderIcon(RefreshOutline)" @click="refresh">
          刷新
        </n-button>
      </template>
    </page-header>

    <n-card size="small">
      <n-space vertical>
        <n-text depth="3" style="font-size: 12px">
          让 Claude Code、Cursor 等 MCP 客户端直接操作本工具：翻译、导出、定时任务等全部功能。
          仅监听本机；可选 token 只保护 /mcp 端点（不影响 WebUI 的 /api）。
        </n-text>
        <n-space align="center">
          <n-text style="font-size: 13px">启用</n-text>
          <n-switch :value="mcp.enabled" @update:value="setMcpEnabled" />
          <n-text v-if="mcp.endpoint" depth="3" style="font-size: 12px">
            端点：{{ mcp.endpoint }}
          </n-text>
          <n-button v-if="mcp.endpoint" size="tiny" quaternary
                    :render-icon="renderIcon(CopyOutline)"
                    @click="copyText(mcp.endpoint)">复制</n-button>
        </n-space>
        <n-space align="center">
          <n-text style="font-size: 13px">工具范围</n-text>
          <n-select :value="mcp.tool_mode" size="small" style="width: 240px"
                    :options="[
                      { label: '精选（30 个高层工具，推荐）', value: 'curated' },
                      { label: '全量（REST 全镜像，上百个）', value: 'full' },
                    ]" @update:value="setMcpMode" />
          <n-text depth="3" style="font-size: 12px">
            切换后客户端需重新连接刷新工具列表
          </n-text>
        </n-space>
        <n-space align="center" wrap>
          <n-text style="font-size: 13px">Token</n-text>
          <n-tag size="small" :type="mcp.has_token ? 'success' : 'default'">
            {{ mcp.has_token ? '已设置' : '未设置' }}
          </n-tag>
          <n-button size="small" :loading="mcpBusy" @click="genMcpToken">
            {{ mcp.has_token ? '重新生成' : '生成' }}
          </n-button>
          <n-button v-if="mcp.has_token" size="small" quaternary
                    @click="clearMcpToken">清除</n-button>
          <n-input v-if="mcpTokenPlain" :value="mcpTokenPlain" readonly
                   size="small" style="width: 320px"
                   @focus="($event.target as HTMLInputElement).select()" />
          <n-button v-if="mcpTokenPlain" size="tiny" quaternary
                    :render-icon="renderIcon(CopyOutline)"
                    @click="copyText(mcpTokenPlain)">复制</n-button>
        </n-space>
        <n-text depth="3" style="font-size: 12px">
          客户端配置示例（Claude Code）：claude mcp add --transport http renpy
          {{ mcp.endpoint || 'http://127.0.0.1:7861/mcp/' }}
          <template v-if="mcp.has_token">
            --header "Authorization: Bearer &lt;token&gt;"
          </template>
          ，详见说明书第 16 章。
        </n-text>
      </n-space>
    </n-card>
  </div>
</template>
