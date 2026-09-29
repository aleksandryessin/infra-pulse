import { useState } from 'react';
import { Button, Layout, Menu, Select, Tooltip } from 'antd';
import type { MenuProps } from 'antd';
import {
  IconChartDots3, IconChartLine, IconChevronLeft, IconChevronRight, IconExternalLink, IconFlask, IconListDetails, IconLogout,
  IconNotebook, IconRuler2, IconUpload, IconUserCircle,
} from '@tabler/icons-react';
import { useLocation, useNavigate } from 'react-router-dom';
import type { Role } from '../../api/auth';
import { useNotifications } from '../../providers/NotificationsProvider';
import { useSession } from '../../providers/SessionProvider';
import { ROLE_LABELS } from '../../shared/config/permissions';
import { GRAFANA_LINK_ENABLED, GRAFANA_SIGNALS_URL, SECTIONS, sectionFor, type SectionKey } from '../../shared/config/routes';
import { MENU_COLLAPSE, MENU_EXPAND, SIGNALS_LINK_HINT, SIGNALS_LINK_LABEL } from '../../shared/config/wording';
import { fmtShortDateTime } from '../../shared/lib/format';
import './SidebarMenu.css';

const ICONS: Record<SectionKey, typeof IconListDetails> = {
  forecast: IconListDetails,
  scheme: IconRuler2,
  journal: IconNotebook,
  research: IconFlask,
  upload: IconUpload,
  messages: IconChartDots3,
};

/** Видимая строка под именем пользователя; у `dev_stub` — только подсказка (F5). */
const AUTH_NOTE: Record<string, string> = {
  fixture: 'Демонстрационная учётная запись',
};
const AUTH_HINT: Record<string, string> = {
  dev_stub: 'Вход не настроен: все запросы идут от демонстрационного пользователя',
};

const COLLAPSED_KEY = 'infra-sidebar-collapsed';

/**
 * Свёрнутое меню помнит этот браузер. Первый вход — развёрнутое меню на любом экране:
 * названия разделов видны сразу, свернуть можно кнопкой у края меню.
 */
function readCollapsed(): boolean {
  try {
    return localStorage.getItem(COLLAPSED_KEY) === '1';
  } catch {
    return false;
  }
}

function saveCollapsed(value: boolean): void {
  try {
    localStorage.setItem(COLLAPSED_KEY, value ? '1' : '0');
  } catch {
    /* localStorage может быть недоступен: состояние живёт до перезагрузки */
  }
}

export function SidebarMenu() {
  const [collapsed, setCollapsed] = useState(readCollapsed);
  const toggle = () => setCollapsed((value) => {
    saveCollapsed(!value);
    return !value;
  });
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const { me, roles, can, devOverrideAllowed, devRole, setDevRole, signOut } = useSession();
  const { newForecasts } = useNotifications();
  const current = sectionFor(pathname);

  const items: MenuProps['items'] = SECTIONS
    .filter((section) => section.inMenu && can(section.permission))
    .map((section) => {
      const Icon = ICONS[section.key];
      return {
        key: section.key,
        icon: <Icon size={18} stroke={1.75} aria-hidden />,
        label: (
          <span className="sidebar__item">
            <span>{section.title}</span>
            {section.key === 'forecast' && newForecasts > 0
              ? <span className="sidebar__count" aria-label={`новые прогнозы: ${newForecasts}`}>новые: {newForecasts}</span>
              : null}
          </span>
        ),
      };
    });

  return (
    <Layout.Sider width={216} collapsedWidth={64} collapsed={collapsed} trigger={null}
      className={`sidebar${collapsed ? ' sidebar--collapsed' : ''}`}>
      <button type="button" className="sidebar__toggle" onClick={toggle} aria-expanded={!collapsed}
        aria-controls="sidebar-sections" aria-label={collapsed ? MENU_EXPAND : MENU_COLLAPSE} title={collapsed ? MENU_EXPAND : MENU_COLLAPSE}>
        {collapsed ? <IconChevronRight size={14} aria-hidden /> : <IconChevronLeft size={14} aria-hidden />}
      </button>
      <div className="sidebar__top">
        <div className="sidebar__logo" title="InfraPulse MSK">{collapsed ? 'IP' : <>InfraPulse <span>MSK</span></>}</div>
        <nav aria-label="Разделы" id="sidebar-sections">
          <Menu
            theme="dark"
            mode="inline"
            inlineCollapsed={collapsed}
            items={items}
            selectedKeys={current ? [current.key] : []}
            onClick={({ key }) => {
              const target = SECTIONS.find((section) => section.key === key);
              if (target) navigate(target.path);
            }}
          />
        </nav>
        {/* Grafana — внешняя ссылка, не раздел SPA: только право research и только при входе через
            каталог (на стенде с LDAP; в fixture и без профиля analytics путь отвечает 502). */}
        {GRAFANA_LINK_ENABLED && me?.auth_source === 'ldap' && can('research') ? (
          <a className="sidebar__external" href={GRAFANA_SIGNALS_URL} target="_blank" rel="noopener noreferrer"
            title={SIGNALS_LINK_HINT} aria-label={SIGNALS_LINK_HINT}>
            <IconChartLine size={18} stroke={1.75} aria-hidden />
            {collapsed ? null : <span>{SIGNALS_LINK_LABEL}</span>}
            {collapsed ? null : <IconExternalLink size={14} stroke={1.75} aria-hidden className="sidebar__external-mark" />}
          </a>
        ) : null}
      </div>

      {collapsed ? (
        <div className="sidebar__bottom sidebar__bottom--collapsed">
          {me ? (
            <Tooltip placement="right" title={`${me.display_name} · ${roles.map((role) => ROLE_LABELS[role]).join(', ')}`}>
              <span className="sidebar__user-icon" tabIndex={0} aria-label={`${me.display_name}: ${roles.map((role) => ROLE_LABELS[role]).join(', ')}`}>
                <IconUserCircle size={22} stroke={1.5} aria-hidden />
              </span>
            </Tooltip>
          ) : null}
          {me?.auth_source === 'ldap' ? (
            <Button size="small" icon={<IconLogout size={16} />} onClick={() => { void signOut(); }} aria-label="Выйти" title="Выйти" />
          ) : null}
        </div>
      ) : (
      <div className="sidebar__bottom">
        {me ? (
          <div className="sidebar__user" title={AUTH_HINT[me.auth_source]}>
            <div className="sidebar__user-name">{me.display_name}</div>
            <div className="sidebar__user-role">{roles.map((role) => ROLE_LABELS[role]).join(', ')}</div>
            {me.auth_source === 'dev_stub' ? null : (
              <div className="sidebar__note">
                {AUTH_NOTE[me.auth_source] ?? (me.session_expires_at ? `Сессия до ${fmtShortDateTime(me.session_expires_at)}` : 'Вход через каталог')}
              </div>
            )}
          </div>
        ) : null}
        {devOverrideAllowed && me ? (
          <label className="sidebar__dev">
            <span>Показать как</span>
            <Select<string>
              size="small"
              value={devRole ?? 'all'}
              onChange={(value) => setDevRole(value === 'all' ? null : (value as Role))}
              options={[
                { value: 'all', label: 'все роли' },
                ...me.roles.map((role) => ({ value: role, label: ROLE_LABELS[role] })),
              ]}
              aria-label="Показать интерфейс как"
            />
          </label>
        ) : null}
        {me?.auth_source === 'ldap' ? (
          <Button size="small" icon={<IconLogout size={16} />} onClick={() => { void signOut(); }}>Выйти</Button>
        ) : null}
      </div>
      )}
    </Layout.Sider>
  );
}
