import { App } from '@modelcontextprotocol/ext-apps'

type ToolResultHandler = (result: unknown) => void

/** Official MCP Apps bridge wrapped behind the app's small existing interface. */
export class LightweightMcpApp {
  private app = new App({ name: 'ytmusic-playlist-manager', version: '1.1.0' })

  constructor(onToolResult: ToolResultHandler) {
    // Claude can send the one-shot initial result as soon as connect completes.
    this.app.ontoolresult = onToolResult
    this.app.onteardown = async () => ({})
  }

  async connect() {
    await this.app.connect()
  }

  callServerTool(name: string, args: Record<string, unknown>) {
    return this.app.callServerTool({ name, arguments: args })
  }
}
