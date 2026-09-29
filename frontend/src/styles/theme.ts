import { theme, type ThemeConfig } from 'antd';

// Принятая палитра C4/C5, см. docs/design/SYSTEM.md.
export const brand = {
  primary: '#2563d7',
  primaryDark: '#80bbff',
};

const shared: ThemeConfig['token'] = {
  fontFamily: 'Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
  fontSize: 14,
  borderRadius: 4,
  colorSuccess: '#21704d',
  colorWarning: '#956000',
  colorError: '#a72e3c',
};

export const lightTheme: ThemeConfig = {
  algorithm: theme.defaultAlgorithm,
  token: {
    ...shared,
    colorPrimary: brand.primary,
    colorBgLayout: '#f4f6f9',
    colorBgContainer: '#ffffff',
    colorText: '#122c48',
    colorTextSecondary: '#536983',
    colorBorder: '#dbe3eb',
  },
  components: {
    Table: { rowHoverBg: '#edf4ff', headerBg: '#f8fafc' },
    Menu: { itemSelectedBg: '#214b74', itemSelectedColor: '#ffffff' },
    Button: { fontWeight: 600, primaryShadow: 'none', defaultShadow: 'none', dangerShadow: 'none' },
  },
};

export const darkTheme: ThemeConfig = {
  algorithm: theme.darkAlgorithm,
  token: {
    ...shared,
    colorPrimary: brand.primaryDark,
    colorBgLayout: '#102537',
    colorBgContainer: '#172f43',
    colorText: '#e8f0f7',
    colorTextSecondary: '#b7c8d9',
    colorBorder: '#355269',
    colorSuccess: '#7ad8b2',
    colorWarning: '#ffe0a0',
    colorError: '#ffc1c8',
  },
  components: {
    Table: { rowHoverBg: '#244a6d', headerBg: '#1c354b' },
    Menu: { itemSelectedBg: '#244a6d', itemSelectedColor: '#ffffff' },
    // Светлая синяя кнопка тёмной темы: тёмный текст вместо белого ради контраста.
    Button: { fontWeight: 600, primaryColor: '#0b2032', primaryShadow: 'none', defaultShadow: 'none', dangerShadow: 'none' },
  },
};
