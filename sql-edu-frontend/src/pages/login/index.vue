<template>
  <view class="login-page">
    <!-- 背景装饰 -->
    <view class="bg-blob bg-blob-1" />
    <view class="bg-blob bg-blob-2" />
    <view class="bg-grid" />

    <view class="login-wrap">
      <!-- 顶部 -->
      <view class="login-top">
        <view class="brand">
          <view class="brand-logo">
            <text class="brand-logo-text">SQL</text>
          </view>
          <view class="brand-info">
            <text class="brand-name">{{ L.appName }}</text>
            <text class="brand-slogan">{{ L.slogan }}</text>
          </view>
        </view>
        <lang-switch />
      </view>

      <!-- 主卡片 -->
      <view class="login-card anim-in">
        <!-- 模式切换 tabs -->
        <view v-if="mode !== 'delete'" class="tabs">
          <view
            class="tab"
            :class="{ active: mode === 'login' }"
            @tap="switchMode('login')"
          >
            {{ L.tabLogin }}
          </view>
          <view
            class="tab"
            :class="{ active: mode === 'register' }"
            @tap="switchMode('register')"
          >
            {{ L.tabRegister }}
          </view>
          <view class="tab-slider" :class="{ right: mode === 'register' }" />
        </view>

        <!-- 登录 -->
        <view v-if="mode === 'login'" class="form">
          <view class="field">
            <text class="field-label">{{ L.account }}</text>
            <input
              v-model="loginForm.email"
              class="input"
              :placeholder="L.accountPlaceholder"
              placeholder-class="ph"
              :maxlength="72"
              confirm-type="next"
            />
          </view>
          <view class="field">
            <text class="field-label">{{ L.password }}</text>
            <view class="pwd-box">
              <input
                v-model="loginForm.password"
                class="input pwd-input"
                :password="!showLoginPwd"
                :placeholder="L.passwordPlaceholder"
                placeholder-class="ph"
                :maxlength="72"
                confirm-type="done"
                @confirm="doLogin"
              />
              <text class="pwd-eye" @tap="showLoginPwd = !showLoginPwd">
                {{ showLoginPwd ? L.hidePwd : L.showPwd }}
              </text>
            </view>
          </view>
          <button class="btn btn-primary btn-block" :loading="loading" :disabled="loading" @tap="doLogin">
            {{ loading ? L.loggingIn : L.tabLogin }}
          </button>
        </view>

        <!-- 注册 -->
        <view v-else-if="mode === 'register'" class="form">
          <view class="field">
            <text class="field-label">{{ L.email }}</text>
            <input
              v-model="registerForm.email"
              class="input"
              placeholder="you@example.com"
              placeholder-class="ph"
              :maxlength="254"
            />
          </view>
          <view class="field">
            <text class="field-label">{{ L.captcha }}</text>
            <view class="captcha-row">
              <input
                v-model="registerForm.captcha"
                class="input captcha-input"
                :placeholder="L.captchaPlaceholder"
                placeholder-class="ph"
                :maxlength="6"
                type="number"
              />
              <button
                class="btn btn-ghost captcha-btn"
                :class="{ 'is-disabled': codeCooldown > 0 || sendingCode }"
                :disabled="codeCooldown > 0 || sendingCode"
                @tap="sendCode"
              >
                {{ codeCooldown > 0 ? `${codeCooldown}s` : L.getCode }}
              </button>
            </view>
          </view>
          <view class="field">
            <text class="field-label">{{ L.username }}</text>
            <input
              v-model="registerForm.username"
              class="input"
              :placeholder="L.usernamePlaceholder"
              placeholder-class="ph"
              :maxlength="64"
            />
          </view>
          <view class="field">
            <text class="field-label">{{ L.password }}</text>
            <view class="pwd-box">
              <input
                v-model="registerForm.password"
                class="input pwd-input"
                :password="!showRegPwd"
                :placeholder="L.passwordRule"
                placeholder-class="ph"
                :maxlength="72"
              />
              <text class="pwd-eye" @tap="showRegPwd = !showRegPwd">
                {{ showRegPwd ? L.hidePwd : L.showPwd }}
              </text>
            </view>
          </view>
          <view class="field">
            <text class="field-label">{{ L.confirmPassword }}</text>
            <input
              v-model="registerForm.confirmPassword"
              class="input"
              :password="true"
              :placeholder="L.confirmPasswordPlaceholder"
              placeholder-class="ph"
              :maxlength="72"
            />
          </view>
          <view class="invite-toggle" @tap="showInvite = !showInvite">
            <text>{{ L.inviteToggle }}</text>
            <text class="invite-arrow">{{ showInvite ? "∧" : "∨" }}</text>
          </view>
          <view v-if="showInvite" class="field">
            <text class="field-label">{{ L.inviteCode }}</text>
            <input
              v-model="registerForm.inviteCode"
              class="input"
              :placeholder="L.invitePlaceholder"
              placeholder-class="ph"
              :maxlength="64"
            />
          </view>
          <button class="btn btn-primary btn-block" :loading="loading" :disabled="loading" @tap="doRegister">
            {{ loading ? L.registering : L.tabRegister }}
          </button>
        </view>

        <!-- 注销账户 -->
        <view v-else class="form">
          <view class="danger-note">
            <text class="danger-note-title">⚠️ {{ L.deleteTitle }}</text>
            <text class="danger-note-desc">{{ L.deleteDesc }}</text>
          </view>
          <view class="field">
            <text class="field-label">{{ L.account }}</text>
            <input
              v-model="deleteForm.email"
              class="input"
              :placeholder="L.accountPlaceholder"
              placeholder-class="ph"
              :maxlength="72"
            />
          </view>
          <view class="field">
            <text class="field-label">{{ L.password }}</text>
            <input
              v-model="deleteForm.password"
              class="input"
              :password="true"
              :placeholder="L.passwordPlaceholder"
              placeholder-class="ph"
              :maxlength="72"
            />
          </view>
          <button class="btn btn-danger btn-block" :loading="loading" :disabled="loading" @tap="doDeleteAccount">
            {{ L.deleteConfirm }}
          </button>
          <button class="btn btn-plain btn-block cancel-btn" @tap="switchMode('login')">
            {{ L.cancel }}
          </button>
        </view>
      </view>

      <!-- 底部链接 -->
      <view v-if="mode !== 'delete'" class="foot-links">
        <text class="foot-link" @tap="switchMode('delete')">{{ L.deleteEntry }}</text>
      </view>
      <text class="foot-version">SQL Learning Lab · Phase 1</text>
    </view>
  </view>
</template>

<script setup lang="ts">
import { computed, onUnmounted, ref } from "vue";
import { onShow } from "@dcloudio/uni-app";
import { deleteAccount, getEmailCode, login, register } from "@/api/auth";
import { language } from "@/utils/i18n";

const STRINGS = {
  "zh-CN": {
    appName: "SQL 智能教学系统",
    slogan: "证据驱动的 SQL 学习与诊断",
    tabLogin: "登录",
    tabRegister: "注册",
    account: "用户名或邮箱",
    accountPlaceholder: "输入用户名或邮箱",
    password: "密码",
    passwordPlaceholder: "输入密码",
    passwordRule: "6～20 位，建议字母 + 数字",
    showPwd: "显示",
    hidePwd: "隐藏",
    email: "邮箱",
    captcha: "邮箱验证码",
    captchaPlaceholder: "6 位验证码",
    getCode: "获取验证码",
    username: "用户名",
    usernamePlaceholder: "给自己起个名字",
    confirmPassword: "确认密码",
    confirmPasswordPlaceholder: "再次输入密码",
    inviteToggle: "教师邀请码（选填）",
    inviteCode: "教师邀请码",
    invitePlaceholder: "填写可注册教师账号",
    loggingIn: "登录中…",
    registering: "注册中…",
    cancel: "取消",
    deleteEntry: "注销账户",
    deleteTitle: "注销不可恢复",
    deleteDesc: "注销将永久删除：账户信息、全部提交记录、学习画像数据。此操作无法撤销。",
    deleteConfirm: "确认注销",
  },
  en: {
    appName: "SQL Learning Lab",
    slogan: "Evidence-driven SQL practice & diagnosis",
    tabLogin: "Sign in",
    tabRegister: "Sign up",
    account: "Username or email",
    accountPlaceholder: "Enter username or email",
    password: "Password",
    passwordPlaceholder: "Enter your password",
    passwordRule: "6–20 characters, letters + digits recommended",
    showPwd: "Show",
    hidePwd: "Hide",
    email: "Email",
    captcha: "Email code",
    captchaPlaceholder: "6-digit code",
    getCode: "Send code",
    username: "Username",
    usernamePlaceholder: "Pick a display name",
    confirmPassword: "Confirm password",
    confirmPasswordPlaceholder: "Repeat your password",
    inviteToggle: "Teacher invite code (optional)",
    inviteCode: "Teacher invite code",
    invitePlaceholder: "Enter to register as teacher",
    loggingIn: "Signing in…",
    registering: "Signing up…",
    cancel: "Cancel",
    deleteEntry: "Delete account",
    deleteTitle: "Deletion is irreversible",
    deleteDesc: "Deleting removes forever: your account, all submissions, and learning profile data. This cannot be undone.",
    deleteConfirm: "Delete permanently",
  },
  "zh-TW": {
    appName: "SQL 智能教學系統",
    slogan: "證據驅動的 SQL 學習與診斷",
    tabLogin: "登入",
    tabRegister: "註冊",
    account: "使用者名稱或信箱",
    accountPlaceholder: "輸入使用者名稱或信箱",
    password: "密碼",
    passwordPlaceholder: "輸入密碼",
    passwordRule: "6～20 位，建議字母 + 數字",
    showPwd: "顯示",
    hidePwd: "隱藏",
    email: "信箱",
    captcha: "信箱驗證碼",
    captchaPlaceholder: "6 位驗證碼",
    getCode: "獲取驗證碼",
    username: "使用者名稱",
    usernamePlaceholder: "給自己起個名字",
    confirmPassword: "確認密碼",
    confirmPasswordPlaceholder: "再次輸入密碼",
    inviteToggle: "教師邀請碼（選填）",
    inviteCode: "教師邀請碼",
    invitePlaceholder: "填寫可註冊教師帳號",
    loggingIn: "登入中…",
    registering: "註冊中…",
    cancel: "取消",
    deleteEntry: "註銷帳戶",
    deleteTitle: "註銷不可恢復",
    deleteDesc: "註銷將永久刪除：帳戶資訊、全部提交記錄、學習畫像資料。此操作無法撤銷。",
    deleteConfirm: "確認註銷",
  },
} as const;

const L = computed(() => STRINGS[language.value]);

type Mode = "login" | "register" | "delete";
const mode = ref<Mode>("login");
const loading = ref(false);
const sendingCode = ref(false);
const codeCooldown = ref(0);
const showInvite = ref(false);
const showLoginPwd = ref(false);
const showRegPwd = ref(false);

const loginForm = ref({ email: "", password: "" });
const registerForm = ref({
  email: "",
  captcha: "",
  username: "",
  password: "",
  confirmPassword: "",
  inviteCode: "",
});
const deleteForm = ref({ email: "", password: "" });

let cooldownTimer: ReturnType<typeof setInterval> | null = null;

onShow(() => {
  // 已登录用户直接进入题库
  if (uni.getStorageSync("token")) {
    uni.reLaunch({ url: "/pages/index/index" });
  }
});

onUnmounted(() => {
  if (cooldownTimer) clearInterval(cooldownTimer);
});

function switchMode(next: Mode) {
  mode.value = next;
}

function startCooldown() {
  codeCooldown.value = 60;
  if (cooldownTimer) clearInterval(cooldownTimer);
  cooldownTimer = setInterval(() => {
    codeCooldown.value -= 1;
    if (codeCooldown.value <= 0 && cooldownTimer) {
      clearInterval(cooldownTimer);
      cooldownTimer = null;
    }
  }, 1000);
}

async function sendCode() {
  const email = registerForm.value.email.trim();
  if (!email) {
    uni.showToast({ title: L.value.email, icon: "none" });
    return;
  }
  sendingCode.value = true;
  try {
    const res = await getEmailCode({ email });
    if (res?.result === "success") {
      uni.showToast({ title: "📬", icon: "none" });
      startCooldown();
    }
  } catch {
    /* request.ts 已统一 toast */
  } finally {
    sendingCode.value = false;
  }
}

async function doLogin() {
  const { email, password } = loginForm.value;
  if (!email.trim() || !password) {
    uni.showToast({ title: L.value.accountPlaceholder, icon: "none" });
    return;
  }
  loading.value = true;
  try {
    const res = await login({ email: email.trim(), password });
    uni.setStorageSync("token", res.token);
    uni.setStorageSync("refresh_token", res.refresh_token);
    uni.setStorageSync("user", res.user);
    uni.showToast({ title: "✨", icon: "none" });
    setTimeout(() => uni.reLaunch({ url: "/pages/index/index" }), 500);
  } catch {
    /* 统一错误处理 */
  } finally {
    loading.value = false;
  }
}

async function doRegister() {
  const f = registerForm.value;
  if (!f.email.trim() || !f.captcha.trim() || !f.username.trim() || !f.password) {
    uni.showToast({ title: L.value.tabRegister, icon: "none" });
    return;
  }
  if (f.password !== f.confirmPassword) {
    uni.showToast({ title: "≠ " + L.value.confirmPassword, icon: "none" });
    return;
  }
  loading.value = true;
  try {
    const res = await register({
      email: f.email.trim(),
      username: f.username.trim(),
      password: f.password,
      confirm_password: f.confirmPassword,
      captcha: f.captcha.trim(),
      invite_code: f.inviteCode.trim() || undefined,
    });
    if (res?.result === "success") {
      uni.showToast({ title: "🎉", icon: "none" });
      loginForm.value.email = f.email.trim();
      loginForm.value.password = "";
      registerForm.value = {
        email: "",
        captcha: "",
        username: "",
        password: "",
        confirmPassword: "",
        inviteCode: "",
      };
      switchMode("login");
    }
  } catch {
    /* 统一错误处理 */
  } finally {
    loading.value = false;
  }
}

async function doDeleteAccount() {
  const f = deleteForm.value;
  if (!f.email.trim() || !f.password) {
    uni.showToast({ title: L.value.accountPlaceholder, icon: "none" });
    return;
  }
  const confirmed = await new Promise<boolean>((resolve) => {
    uni.showModal({
      title: L.value.deleteTitle,
      content: L.value.deleteDesc,
      confirmText: L.value.deleteConfirm,
      confirmColor: "#E5484D",
      success: (r) => resolve(r.confirm),
      fail: () => resolve(false),
    });
  });
  if (!confirmed) return;

  loading.value = true;
  try {
    // 先验证身份拿到 token，再执行注销
    const loginRes = await login({ email: f.email.trim(), password: f.password });
    uni.setStorageSync("token", loginRes.token);
    uni.setStorageSync("refresh_token", loginRes.refresh_token);
    await deleteAccount({ password: f.password });
    uni.removeStorageSync("token");
    uni.removeStorageSync("refresh_token");
    uni.removeStorageSync("user");
    deleteForm.value = { email: "", password: "" };
    uni.showToast({ title: "👋", icon: "none" });
    switchMode("login");
  } catch {
    uni.removeStorageSync("token");
    uni.removeStorageSync("refresh_token");
    uni.removeStorageSync("user");
  } finally {
    loading.value = false;
  }
}
</script>

<style lang="scss" scoped>
.login-page {
  min-height: 100vh;
  background: linear-gradient(180deg, #eef2ff 0%, #f4f6fb 46%, #f4f6fb 100%);
  position: relative;
  overflow: hidden;
}

.bg-blob {
  position: absolute;
  border-radius: 50%;
  filter: blur(90rpx);
  opacity: 0.5;
}

.bg-blob-1 {
  width: 520rpx;
  height: 520rpx;
  background: #c9d6ff;
  top: -160rpx;
  right: -140rpx;
}

.bg-blob-2 {
  width: 420rpx;
  height: 420rpx;
  background: #e3d8ff;
  top: 300rpx;
  left: -180rpx;
}

.bg-grid {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 560rpx;
  background-image:
    linear-gradient(rgba(76, 111, 255, 0.05) 1px, transparent 1px),
    linear-gradient(90deg, rgba(76, 111, 255, 0.05) 1px, transparent 1px);
  background-size: 44rpx 44rpx;
  mask-image: linear-gradient(180deg, #000 0%, transparent 100%);
  -webkit-mask-image: linear-gradient(180deg, #000 0%, transparent 100%);
}

.login-wrap {
  position: relative;
  z-index: 1;
  max-width: 700px;
  margin: 0 auto;
  padding: 100rpx 44rpx calc(60rpx + env(safe-area-inset-bottom));
  box-sizing: border-box;
}

.login-top {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  margin-bottom: 56rpx;
}

.brand {
  display: flex;
  align-items: center;
  gap: 22rpx;
}

.brand-logo {
  width: 96rpx;
  height: 96rpx;
  border-radius: 28rpx;
  background: $brand-gradient;
  display: flex;
  align-items: center;
  justify-content: center;
  box-shadow: 0 16rpx 36rpx rgba(76, 111, 255, 0.35);

  .brand-logo-text {
    color: #fff;
    font-weight: 800;
    font-size: 30rpx;
    letter-spacing: 1rpx;
    font-family: $font-mono;
  }
}

.brand-name {
  display: block;
  font-size: 38rpx;
  font-weight: 800;
  color: $text-1;
}

.brand-slogan {
  display: block;
  margin-top: 4rpx;
  font-size: 24rpx;
  color: $text-3;
}

.login-card {
  background: rgba(255, 255, 255, 0.92);
  backdrop-filter: blur(20px);
  border-radius: 36rpx;
  box-shadow: 0 24rpx 80rpx rgba(23, 29, 43, 0.1);
  padding: 40rpx 40rpx 44rpx;
}

.tabs {
  position: relative;
  display: flex;
  background: #eef1f8;
  border-radius: 999rpx;
  padding: 8rpx;
  margin-bottom: 40rpx;
}

.tab {
  flex: 1;
  text-align: center;
  font-size: 28rpx;
  font-weight: 600;
  color: $text-2;
  padding: 16rpx 0;
  position: relative;
  z-index: 1;
  transition: color 0.2s ease;

  &.active {
    color: $brand-deep;
  }
}

.tab-slider {
  position: absolute;
  top: 8rpx;
  bottom: 8rpx;
  left: 8rpx;
  width: calc(50% - 8rpx);
  background: #fff;
  border-radius: 999rpx;
  box-shadow: 0 4rpx 12rpx rgba(23, 29, 43, 0.1);
  transition: transform 0.22s ease;

  &.right {
    transform: translateX(100%);
  }
}

.form {
  display: block;
}

.captcha-row {
  display: flex;
  gap: 16rpx;
  align-items: stretch;

  .captcha-input {
    flex: 1;
    min-width: 0;
    letter-spacing: 4rpx;
    font-family: $font-mono;
  }

  .captcha-btn {
    flex-shrink: 0;
    font-size: 24rpx;
    padding: 0 26rpx;
    border-radius: 16rpx;
  }
}

.pwd-box {
  position: relative;

  .pwd-input {
    padding-right: 88rpx;
  }

  .pwd-eye {
    position: absolute;
    right: 16rpx;
    top: 50%;
    transform: translateY(-50%);
    font-size: 22rpx;
    color: $brand-deep;
    background: $brand-soft;
    border-radius: 999rpx;
    padding: 8rpx 20rpx;
  }
}

.invite-toggle {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 24rpx;
  color: $text-3;
  padding: 6rpx 4rpx 20rpx;

  .invite-arrow {
    font-size: 22rpx;
  }
}

.danger-note {
  background: $danger-soft;
  border: 2rpx solid rgba(229, 72, 77, 0.18);
  border-radius: 18rpx;
  padding: 24rpx 26rpx;
  margin-bottom: 30rpx;

  .danger-note-title {
    display: block;
    font-size: 27rpx;
    font-weight: 700;
    color: $danger;
  }

  .danger-note-desc {
    display: block;
    margin-top: 8rpx;
    font-size: 24rpx;
    color: #b23b40;
    line-height: 1.65;
  }
}

.cancel-btn {
  margin-top: 18rpx;
}

.foot-links {
  margin-top: 32rpx;
  text-align: center;

  .foot-link {
    font-size: 24rpx;
    color: $text-3;
    text-decoration: underline;
  }
}

.foot-version {
  display: block;
  margin-top: 36rpx;
  text-align: center;
  font-size: 22rpx;
  color: $text-3;
  font-family: $font-mono;
  letter-spacing: 1rpx;
}
</style>
