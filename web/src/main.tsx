import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App.tsx'
import { AuthGate } from './components/AuthGate.tsx'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <AuthGate renderPublic={(showcases, onLogin) => <App publicShowcases={showcases} onLogin={onLogin} />}>
      {(account, onLogout) => <App account={account} onLogout={onLogout} />}
    </AuthGate>
  </StrictMode>,
)
