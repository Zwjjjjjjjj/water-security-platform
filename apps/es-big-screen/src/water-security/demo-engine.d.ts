declare module '@/water-security/integration/demo-engine.js' {
  export const DATA_FILES: string[]
  export function loadDemoData(base?: string): Promise<any>
  export function clampTime(data: any, time: any): number
  export function interpolate(samples: any[], time: number): any
  export function historyAt(samples: any[], time: number, activeFrom?: number): any[]
  export function getDemoSnapshot(data: any, time: number): any
  export function getScriptedState(data: any, time: number): any
  export function pathD(points: any[], width?: number, height?: number): string
  export function formatTime(ms: number): string
}
