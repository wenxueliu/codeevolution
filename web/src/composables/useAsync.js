import { reactive } from 'vue'

export function useAsync(initialData = null) {
  const state = reactive({ data: initialData, loading: false, error: null })

  async function execute(task) {
    state.loading = true
    state.error = null
    try {
      state.data = await task()
      return state.data
    } catch (error) {
      state.error = error
      throw error
    } finally {
      state.loading = false
    }
  }

  return { state, execute }
}

export async function runAsync(component, task) {
  component.loading = true
  component.error = null
  try {
    return await task()
  } catch (error) {
    component.error = error
    return undefined
  } finally {
    component.loading = false
  }
}
