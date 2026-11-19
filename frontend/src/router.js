import { createRouter, createWebHistory } from 'vue-router'
import Loom from './pages/Loom.vue'
import Ingest from './pages/Ingest.vue'
import Quarantine from './pages/Quarantine.vue'
import Proofs from './pages/Proofs.vue'
import Campaign from './pages/Campaign.vue'
import Settings from './pages/Settings.vue'

export default createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', component: Loom },
    { path: '/ingest', component: Ingest },
    { path: '/quarantine', component: Quarantine },
    { path: '/proofs', component: Proofs },
    { path: '/campaign', component: Campaign },
    { path: '/settings', component: Settings },
  ],
})
