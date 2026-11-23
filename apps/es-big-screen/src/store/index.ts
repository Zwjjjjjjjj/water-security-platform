import { type App } from 'vue'
import { createPinia } from 'pinia'

export * from './screen'
export * from './water'

export const install = (app: App) => {
	app.use(createPinia())
}
