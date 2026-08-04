<script setup lang="ts">
import { useRoute } from 'vue-router'
import { NEmpty } from 'naive-ui'

const route = useRoute()
</script>

<template>
  <n-empty :description="`${route.meta.title ?? '该页面'} 将在后续阶段迁移`" style="margin-top: 80px" />
</template>
