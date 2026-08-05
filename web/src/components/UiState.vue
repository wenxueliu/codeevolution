<template>
  <div :class="['ui-state', `is-${kind}`]" :role="kind === 'error' ? 'alert' : 'status'">
    <strong>{{ title }}</strong>
    <p v-if="message">{{ message }}</p>
    <div class="ui-state-actions" v-if="actionLabel || dismissLabel">
      <button v-if="actionLabel" type="button" class="action-btn" @click="$emit('action')">{{ actionLabel }}</button>
      <button v-if="dismissLabel" type="button" class="dismiss-btn" @click="$emit('dismiss')">{{ dismissLabel }}</button>
    </div>
    <slot />
  </div>
</template>

<script>
export default {
  name: 'UiState',
  emits: ['action', 'dismiss'],
  props: {
    kind: { type: String, default: 'empty' },
    title: { type: String, required: true },
    message: { type: String, default: '' },
    actionLabel: { type: String, default: '' },
    dismissLabel: { type: String, default: '' },
  },
}
</script>

<style scoped>
.ui-state { border: 1px solid #e2e5eb; border-radius: 8px; padding: 18px; background: #f8f9fb; color: #4d5665; }
.ui-state strong { display: block; color: #252b36; margin-bottom: 4px; }
.ui-state p { margin: 0; font-size: 13px; line-height: 1.55; }
.ui-state-actions { display: flex; gap: 8px; margin-top: 12px; }
.action-btn { border: 0; border-radius: 6px; padding: 8px 12px; background: #e94560; color: white; cursor: pointer; }
.dismiss-btn { border: 1px solid #cfd3da; border-radius: 6px; padding: 8px 12px; background: #fff; color: #555; cursor: pointer; }
.dismiss-btn:hover { background: #f5f5f7; }
.is-error { border-color: #efc4cb; background: #fff4f5; color: #8d2437; }
.is-loading { border-color: #dce2ec; background: #f4f7fb; }
</style>
