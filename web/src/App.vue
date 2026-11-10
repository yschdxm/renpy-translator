<script setup lang="ts">
import { NConfigProvider, NGlobalStyle, NMessageProvider, NDialogProvider, darkTheme, zhCN, dateZhCN } from 'naive-ui'
import AppInner from './AppInner.vue'
import { overrides } from './theme/overrides'
import { t } from './theme/tokens'

/** 把 token 注入为 CSS 变量（--rt-<name>）：样式块/内联样式的唯一色值来源。
 *  故意不写字面量进 CSS——hex 只允许存在于 theme/tokens.ts（清扫验收依据）。 */
const cssVars = Object.entries(t)
  .map(([k, v]) => `--rt-${k}:${v}`)
  .join(';')
const styleEl = document.createElement('style')
styleEl.textContent = `:root{${cssVars}}`
document.head.appendChild(styleEl)
</script>

<template>
  <n-config-provider :theme="darkTheme" :theme-overrides="overrides"
                     :locale="zhCN" :date-locale="dateZhCN">
    <n-global-style />
    <n-message-provider>
      <n-dialog-provider>
        <app-inner />
      </n-dialog-provider>
    </n-message-provider>
  </n-config-provider>
</template>

<style>
/* 全局滚动条：统一暗色细圆角，与 naive-ui n-scrollbar 观感一致。
   覆盖所有原生 overflow 区域（对话框、日志、表格、textarea 等），
   不再出现 Windows 系统亮色滚动条。
   只用 ::-webkit-scrollbar（应用跑在 WebView2 / Chrome / Edge）；
   不写 scrollbar-width——Chromium 两者并存时会禁用伪元素定制。 */
::-webkit-scrollbar {
  width: 8px;
  height: 8px;
}
::-webkit-scrollbar-track {
  background: transparent;
}
::-webkit-scrollbar-thumb {
  background: rgba(255, 255, 255, 0.16);
  border-radius: 4px;
}
::-webkit-scrollbar-thumb:hover {
  background: rgba(255, 255, 255, 0.28);
}
::-webkit-scrollbar-corner {
  background: transparent;
}

/* 键盘焦点环（= tokens.ts 的 t.focus，全局唯一焦点表达） */
:focus-visible {
  outline: 2px solid rgba(99, 226, 183, 0.45);
  outline-offset: 1px;
}

/* 语义工具类：内联 color 的统一替代（清扫验收的一部分）。
   命名与 tokens.ts 对应；新增语义先加 token 再加类，不写裸色值。 */
.rt-t1 { color: var(--rt-text1); }
.rt-t2 { color: var(--rt-text2); }
.rt-t3 { color: var(--rt-text3); }
.rt-primary { color: var(--rt-primary); }
.rt-warning { color: var(--rt-warning); }
.rt-error { color: var(--rt-error); }
.rt-info { color: var(--rt-info); }
.rt-markfg { color: var(--rt-markFg); }
.rt-fs12 { font-size: 12px; }
.rt-fs13 { font-size: 13px; }

/* 模态框长列表统一约定：
   - n-modal 一律 width: min(Xpx, 96vw)，窄窗不横向溢出；
   - 长列表/长内容包进 .modal-scroll——只滚内容区，标题与页脚固定可见；
   - 不要给整张卡设 overflow（标题/按钮会跟着滚走）。
   高度用视口占比，替代各处写死的 px/vh。 */
.modal-scroll {
  max-height: 60vh;
  overflow-y: auto;
}

/* 页面工具栏吸顶 / 分页吸底：长列表滚动时功能菜单与翻页始终可见。
   不透明背景防表格内容透出；祖先不得新增 overflow（sticky 会失效）；
   z-index 远低于 Naive 弹出层与全局状态栏(200)。
   分页 bottom:26px = 状态栏高度，吸附时停在状态栏上方。 */
.rt-page-toolbar {
  position: sticky;
  top: 0;
  z-index: 30;
  background: var(--rt-surface0);
  border-bottom: 1px solid var(--rt-border);
  padding: 8px 0;
  /* 实体阴影延伸背景：上盖滚动容器 16px padding 区、下盖 margin 间隙，
     否则滚动内容会从这些缝隙透出 */
  box-shadow: 0 -16px 0 var(--rt-surface0), 0 12px 0 var(--rt-surface0);
}
.rt-page-pager {
  position: sticky;
  bottom: 26px;
  z-index: 25;
  background: var(--rt-surface0);
  border-top: 1px solid var(--rt-border);
  padding: 8px 0;
  /* 上盖 margin 间隙、下盖容器底部 padding 区（状态栏 z-index 更高不受影响） */
  box-shadow: 0 -10px 0 var(--rt-surface0), 0 40px 0 var(--rt-surface0);
}
</style>
