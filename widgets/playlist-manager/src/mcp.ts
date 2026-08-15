type ToolResultHandler = (result: unknown) => void

type PendingRequest = {
  resolve: (value: unknown) => void
  reject: (reason: Error) => void
  timer: number
}

/** Small MCP Apps postMessage bridge. Keeps the single-file template host-friendly. */
export class LightweightMcpApp {
  private nextId = 1
  private pending = new Map<number, PendingRequest>()
  private listening = false

  constructor(private onToolResult: ToolResultHandler) {}

  private post(message: unknown) {
    window.parent.postMessage(message, '*')
  }

  private onMessage = (event: MessageEvent) => {
    if (event.source !== window.parent) return
    const message = event.data
    if (!message || message.jsonrpc !== '2.0') return

    if (typeof message.id === 'number' && this.pending.has(message.id)) {
      const pending = this.pending.get(message.id)!
      window.clearTimeout(pending.timer)
      this.pending.delete(message.id)
      if (message.error) pending.reject(new Error(message.error.message || 'Host request failed'))
      else pending.resolve(message.result)
      return
    }

    if (message.method === 'ui/notifications/tool-result') {
      this.onToolResult(message.params)
    } else if (message.method === 'ui/resource-teardown' && message.id !== undefined) {
      this.post({ jsonrpc: '2.0', id: message.id, result: {} })
    }
  }

  private request(method: string, params: Record<string, unknown>) {
    const id = this.nextId++
    return new Promise<unknown>((resolve, reject) => {
      const timer = window.setTimeout(() => {
        this.pending.delete(id)
        reject(new Error(`Host did not answer ${method}`))
      }, 15000)
      this.pending.set(id, { resolve, reject, timer })
      this.post({ jsonrpc: '2.0', id, method, params })
    })
  }

  async connect() {
    if (!this.listening) {
      window.addEventListener('message', this.onMessage)
      this.listening = true
    }
    await this.request('ui/initialize', {
      protocolVersion: '2026-01-26',
      appInfo: { name: 'ytmusic-playlist-manager', version: '1.0.0' },
      appCapabilities: { availableDisplayModes: ['inline', 'fullscreen'] },
    })
    this.post({ jsonrpc: '2.0', method: 'ui/notifications/initialized', params: {} })
  }

  callServerTool(name: string, args: Record<string, unknown>) {
    return this.request('tools/call', { name, arguments: args })
  }
}
