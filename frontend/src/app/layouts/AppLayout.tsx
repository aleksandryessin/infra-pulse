import { Suspense } from 'react';
import { Layout } from 'antd';
import { Outlet } from 'react-router-dom';
import { ForecastStateProvider } from '../../providers/ForecastStateProvider';
import { NotificationsProvider } from '../../providers/NotificationsProvider';
import { StateMessage } from '../../shared/ui/StateMessage';
import { AppHeader } from '../../widgets/header/Header';
import { SidebarMenu } from '../../widgets/sidebar-menu/SidebarMenu';

export function AppLayout() {
  return (
    <ForecastStateProvider>
      <NotificationsProvider>
      <Layout style={{ minHeight: '100vh' }}>
        <SidebarMenu />
        <Layout>
          <AppHeader />
          <Layout.Content className="app-content">
            <Suspense fallback={<StateMessage kind="loading" title="Загрузка раздела…" />}>
              <Outlet />
            </Suspense>
          </Layout.Content>
          <div className="app-footer">
            <span>Поддержка решений диспетчера. Штатная сигнализация и регламенты сохраняют приоритет.</span>
            <span>InfraPulse MSK</span>
          </div>
        </Layout>
      </Layout>
      </NotificationsProvider>
    </ForecastStateProvider>
  );
}
