<template>
  <div :class="['ui-state', `is-${kind}`]" :role="kind === 'error' ? 'alert' : 'status'">
    <strong>{{ title }}</strong>
    <p v-if="message">{{ message }}</p>
    <button v-if="actionLabel" type="button" @click="$emit('action')">{{ actionLabel }}</button>
    <slot />
  </div>
</template>

<script>
export default {
  name: 'UiState',
  emits: ['action'],
  props: {
    kind: { type: String, default: 'empty' },
    title: { type: String, required: true },
    message: { type: String, default: '' },
    actionLabel: { type: String, default: '' },
  },
}
</script>

<style scoped>
.ui-state { border: 1px solid #e2e5eb; border-radius: 8px; padding: 18px; background: #f8f9fb; color: #4d5665; }
.ui-state strong { display: block; color: #252b36; margin-bottom: 4px; }
.ui-state p { margin: 0; font-size: 13px; line-height: 1.55; }
.ui-state button { margin-top: 12px; border: 0; border-radius: 6px; padding: 8px 12px; background: #e94560; color: white; cursor: pointer; }
.is-error { border-color: #efc4cb; background: #fff4f5; color: #8d2437; }
.is-loading { border-color: #dce2ec; background: #f4f7fb; }
</style>
