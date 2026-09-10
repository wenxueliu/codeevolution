export const LAST_SNAPSHOT_ID_KEY = 'codeevolution:last-snapshot-id'
export const LAST_SNAPSHOT_MEMBER_KEY = 'codeevolution:last-snapshot-member'
export const LAST_GRAPH_VIEW_ID_KEY = 'codeevolution:last-graph-view-id'
export const NAVIGATION_CONTEXT_EVENT = 'codeevolution:navigation-context'

export function readNavigationContext() {
  if (typeof window === 'undefined') return { snapshotId: '', snapshotMember: '', viewId: '' }
  return {
    snapshotId: window.sessionStorage?.getItem(LAST_SNAPSHOT_ID_KEY) || '',
    snapshotMember: window.sessionStorage?.getItem(LAST_SNAPSHOT_MEMBER_KEY) || '',
    viewId: window.sessionStorage?.getItem(LAST_GRAPH_VIEW_ID_KEY) || '',
  }
}

export function rememberNavigationContext({ snapshotId, snapshotMember, viewId } = {}) {
  if (typeof window === 'undefined') return { snapshotId: '', snapshotMember: '', viewId: '' }
  const storage = window.sessionStorage
  if (snapshotId !== undefined) {
    if (snapshotId) storage?.setItem(LAST_SNAPSHOT_ID_KEY, snapshotId)
    else storage?.removeItem(LAST_SNAPSHOT_ID_KEY)
  }
  if (snapshotMember !== undefined) {
    if (snapshotMember) storage?.setItem(LAST_SNAPSHOT_MEMBER_KEY, snapshotMember)
    else storage?.removeItem(LAST_SNAPSHOT_MEMBER_KEY)
  }
  if (viewId !== undefined) {
    if (viewId) storage?.setItem(LAST_GRAPH_VIEW_ID_KEY, viewId)
    else storage?.removeItem(LAST_GRAPH_VIEW_ID_KEY)
  }
  const context = readNavigationContext()
  window.dispatchEvent(new CustomEvent(NAVIGATION_CONTEXT_EVENT, { detail: context }))
  return context
}
