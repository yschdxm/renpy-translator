<script setup lang="ts">
import { NConfigProvider, NMessageProvider, NDialogProvider, darkTheme, zhCN, dateZhCN } from 'naive-ui'
import AppInner from './AppInner.vue'
</script>

<template>
  <n-config-provider :theme="darkTheme" :locale="zhCN" :date-locale="dateZhCN">
    <n-message-provider>
      <n-dialog-provider>
        <app-inner />
      </n-dialog-provider>
    </n-message-provider>
  </n-config-provider>
</template>
