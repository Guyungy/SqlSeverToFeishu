// esbuild 遇到从 TS 里 import 的 CSS 会把它抽成独立的 main.css，
// TypeScript 这边不需要知道内容，只需要知道这个模块存在。
declare module '*.css';

// 桌面运行时由 tauri.conf.json 的 `app.withGlobalTauri` 挂到 window 上，
// 这里只声明我们实际用到的那一小块，具体封装见 api.ts。
interface Window {
  __TAURI__?: {
    core?: {
      invoke<T>(command: string, args?: Record<string, unknown>): Promise<T>;
    };
  };
}
