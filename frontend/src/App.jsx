import { useCallback, useEffect, useState } from 'react'
import AdminBusinessRulesPage from './pages/AdminBusinessRulesPage.jsx'
import ChatPage from './pages/ChatPage.jsx'

export default function App() {
  const [pathname, setPathname] = useState(window.location.pathname)

  useEffect(() => {
    const handlePopState = () => setPathname(window.location.pathname)
    window.addEventListener('popstate', handlePopState)
    return () => window.removeEventListener('popstate', handlePopState)
  }, [])

  const navigate = useCallback((path) => {
    if (window.location.pathname !== path) {
      window.history.pushState({}, '', path)
    }
    setPathname(path)
  }, [])

  if (pathname === '/admin/business-rules') {
    return <AdminBusinessRulesPage onNavigate={navigate} />
  }

  return <ChatPage onNavigate={navigate} />
}
