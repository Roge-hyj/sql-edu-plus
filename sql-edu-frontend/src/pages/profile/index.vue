<template>
  <view class="page profile-page">
    <app-navbar :title="L.title">
      <template #right>
        <lang-switch />
      </template>
    </app-navbar>

    <!-- 用户卡 -->
    <view class="profile-hero anim-in">
      <view class="profile-avatar">
        <text class="profile-avatar-text">{{ avatarChar }}</text>
      </view>
      <view class="profile-info">
        <view class="profile-name-row">
          <text class="profile-name">{{ user?.username || "—" }}</text>
          <view class="role-chip" :class="{ teacher: isTeacher }">
            {{ isTeacher ? L.teacher : L.student }}
          </view>
        </view>
        <text class="profile-email">{{ user?.email || "" }}</text>
      </view>
    </view>

    <!-- 学习画像 -->
    <view class="card">
      <view class="card-title">
        <view class="dot" />
        <text>{{ L.masteryTitle }}</text>
        <text class="extra">{{ masterySummary }}</text>
      </view>
      <text class="mastery-desc">{{ L.masteryDesc }}</text>

      <view v-if="loadingMastery" class="loading-box">
        <text class="anim-pulse">{{ L.loading }}</text>
      </view>
      <template v-else>
        <view v-for="group in masteryGroups" :key="group.key" class="mastery-group">
          <view class="mastery-group-head">
            <text class="mastery-group-title">{{ group.label() }}</text>
            <text class="mastery-group-avg mono">{{ group.avgText }}</text>
          </view>
          <view v-for="item in group.items" :key="item.id" class="mastery-row">
            <text class="mastery-name" :class="{ unobserved: !item.observed }">
              {{ item.name }}
            </text>
            <view class="mastery-bar">
              <view
                class="mastery-fill"
                :class="{ unobserved: !item.observed }"
                :style="{ width: `${Math.round(item.value * 100)}%` }"
              />
            </view>
            <text class="mastery-value mono" :class="{ unobserved: !item.observed }">
              {{ item.valueText }}
            </text>
          </view>
        </view>
      </template>
    </view>

    <!-- 账号设置 -->
    <view class="card">
      <view class="card-title">
        <view class="dot" />
        <text>{{ L.settingsTitle }}</text>
      </view>

      <!-- 用户名 -->
      <view class="field">
        <text class="field-label">{{ L.usernameLabel }}</text>
        <view class="inline-row">
          <input
            v-model="usernameDraft"
            class="input inline-input"
            :placeholder="user?.username || ''"
            placeholder-class="ph"
            :maxlength="64"
          />
          <button
            class="btn btn-ghost btn-sm"
            :class="{ 'is-disabled': savingUsername || !usernameDraft.trim() || usernameDraft.trim() === user?.username }"
            :disabled="savingUsername || !usernameDraft.trim() || usernameDraft.trim() === user?.username"
            @tap="saveUsername"
          >
            {{ L.update }}
          </button>
        </view>
      </view>

      <!-- 修改密码 -->
      <view class="collapse-toggle" @tap="pwdOpen = !pwdOpen">
        <text>{{ L.changePassword }}</text>
        <text class="collapse-arrow">{{ pwdOpen ? "∧" : "∨" }}</text>
      </view>
      <view v-if="pwdOpen" class="pwd-form">
        <view class="field">
          <text class="field-label">{{ L.oldPassword }}</text>
          <input v-model="pwdForm.oldPassword" class="input" :password="true" :maxlength="72" />
        </view>
        <view class="field">
          <text class="field-label">{{ L.newPassword }}</text>
          <input v-model="pwdForm.newPassword" class="input" :password="true" :maxlength="72" />
        </view>
        <view class="field">
          <text class="field-label">{{ L.confirmPassword }}</text>
          <input v-model="pwdForm.confirmPassword" class="input" :password="true" :maxlength="72" />
        </view>
        <button
          class="btn btn-primary btn-block btn-sm"
          :class="{ 'is-disabled': savingPwd || !pwdValid }"
          :disabled="savingPwd || !pwdValid"
          @tap="savePassword"
        >
          {{ savingPwd ? L.saving : L.update }}
        </button>
      </view>
    </view>

    <!-- 退出登录 -->
    <button class="btn btn-plain btn-block logout-btn" @tap="confirmLogout">
      {{ L.logout }}
    </button>

    <!-- 危险区 -->
    <view class="danger-zone">
      <text class="danger-zone-title">{{ L.dangerZone }}</text>
      <text class="danger-zone-desc">{{ L.deleteDesc }}</text>
      <view v-if="deleteOpen" class="field delete-pwd-field">
        <text class="field-label">{{ L.password }}</text>
        <input v-model="deletePassword" class="input" :password="true" :maxlength="72" />
      </view>
      <button
        v-if="deleteOpen"
        class="btn btn-danger btn-block btn-sm"
        :class="{ 'is-disabled': deleting || !deletePassword }"
        :disabled="deleting || !deletePassword"
        @tap="confirmDeleteAccount"
      >
        {{ deleting ? L.saving : L.deleteConfirm }}
      </button>
      <text v-else class="danger-zone-link" @tap="deleteOpen = true">{{ L.deleteEntry }}</text>
    </view>
  </view>
</template>

<script setup lang="ts">
import { computed, ref } from "vue";
import { onShow } from "@dcloudio/uni-app";
import {
  changePassword,
  deleteAccount,
  getProfile,
  logout,
  updateProfile,
} from "@/api/auth";
import { getMasteryRadar } from "@/api/ai";
import { ensureAuthed } from "@/utils/auth";
import { language } from "@/utils/i18n";
import { KNOWLEDGE_META, knowledgeName, knowledgeLevel } from "@/utils/knowledge";
import type { MasteryRadar, UserSchema } from "@/types";

const STRINGS = {
  "zh-CN": {
    title: "我的",
    teacher: "教师",
    student: "学生",
    masteryTitle: "学习画像",
    masteryDesc: "基于贝叶斯知识追踪（BKT）的后验掌握度。浅色条为尚未练习的知识点（先验值）。",
    masteryObserved: "已观测",
    masterySummaryOne: "{n} 个知识点有练习记录",
    masterySummaryNone: "还没有练习记录",
    loading: "加载中…",
    settingsTitle: "账号设置",
    usernameLabel: "用户名",
    update: "更新",
    saving: "保存中…",
    changePassword: "修改密码",
    oldPassword: "旧密码",
    newPassword: "新密码",
    confirmPassword: "确认新密码",
    password: "密码",
    logout: "退出登录",
    logoutConfirm: "确定退出当前账号吗？",
    dangerZone: "危险区",
    deleteDesc: "注销账户将永久删除账户信息、全部提交记录与学习画像，不可恢复。",
    deleteEntry: "注销账户",
    deleteConfirm: "确认永久注销",
    usernameUpdated: "用户名已更新",
    passwordUpdated: "密码已修改，请重新登录",
    passwordMismatch: "两次输入的新密码不一致",
    passwordRequired: "请填写完整密码信息",
  },
  en: {
    title: "Me",
    teacher: "Teacher",
    student: "Student",
    masteryTitle: "Learning profile",
    masteryDesc: "Posterior mastery from Bayesian Knowledge Tracing. Light bars are unpracticed points (prior).",
    masteryObserved: "observed",
    masterySummaryOne: "{n} knowledge points practiced",
    masterySummaryNone: "No practice records yet",
    loading: "Loading…",
    settingsTitle: "Account settings",
    usernameLabel: "Username",
    update: "Update",
    saving: "Saving…",
    changePassword: "Change password",
    oldPassword: "Current password",
    newPassword: "New password",
    confirmPassword: "Confirm new password",
    password: "Password",
    logout: "Sign out",
    logoutConfirm: "Sign out of this account?",
    dangerZone: "Danger zone",
    deleteDesc: "Deleting your account permanently removes your profile, all submissions and learning data.",
    deleteEntry: "Delete account",
    deleteConfirm: "Delete permanently",
    usernameUpdated: "Username updated",
    passwordUpdated: "Password changed, please sign in again",
    passwordMismatch: "New passwords do not match",
    passwordRequired: "Please fill in all password fields",
  },
  "zh-TW": {
    title: "我的",
    teacher: "教師",
    student: "學生",
    masteryTitle: "學習畫像",
    masteryDesc: "基於貝葉斯知識追蹤（BKT）的後驗掌握度。淺色條為尚未練習的知識點（先驗值）。",
    masteryObserved: "已觀測",
    masterySummaryOne: "{n} 個知識點有練習記錄",
    masterySummaryNone: "還沒有練習記錄",
    loading: "載入中…",
    settingsTitle: "帳號設定",
    usernameLabel: "使用者名稱",
    update: "更新",
    saving: "保存中…",
    changePassword: "修改密碼",
    oldPassword: "舊密碼",
    newPassword: "新密碼",
    confirmPassword: "確認新密碼",
    password: "密碼",
    logout: "登出",
    logoutConfirm: "確定登出當前帳號嗎？",
    dangerZone: "危險區",
    deleteDesc: "註銷帳戶將永久刪除帳戶資訊、全部提交記錄與學習畫像，不可恢復。",
    deleteEntry: "註銷帳戶",
    deleteConfirm: "確認永久註銷",
    usernameUpdated: "使用者名稱已更新",
    passwordUpdated: "密碼已修改，請重新登入",
    passwordMismatch: "兩次輸入的新密碼不一致",
    passwordRequired: "請填寫完整密碼資訊",
  },
} as const;

const L = computed(() => STRINGS[language.value]);

const user = ref<UserSchema | null>(null);
const mastery = ref<MasteryRadar | null>(null);
const loadingMastery = ref(true);

const usernameDraft = ref("");
const savingUsername = ref(false);

const pwdOpen = ref(false);
const savingPwd = ref(false);
const pwdForm = ref({ oldPassword: "", newPassword: "", confirmPassword: "" });

const deleteOpen = ref(false);
const deletePassword = ref("");
const deleting = ref(false);

const isTeacher = computed(() => user.value?.role === "teacher");
const avatarChar = computed(() => (user.value?.username || "?").slice(0, 1).toUpperCase());

const pwdValid = computed(() =>
  Boolean(pwdForm.value.oldPassword && pwdForm.value.newPassword && pwdForm.value.confirmPassword),
);

/* ---------- 学习画像 ---------- */

type MasteryItem = {
  id: string;
  name: string;
  value: number;
  valueText: string;
  observed: boolean;
};

const masteryItems = computed<MasteryItem[]>(() => {
  const state = mastery.value?.mastery_state ?? {};
  const observedIds = new Set(
    (mastery.value?.state_details ?? [])
      .filter((d) => d.taxonomy_version && d.observation_count > 0)
      .map((d) => d.skill_id),
  );
  return KNOWLEDGE_META.map((meta) => {
    const raw = state[meta.id];
    const observed = observedIds.has(meta.id);
    const value = typeof raw === "number" ? Math.max(0, Math.min(1, raw)) : 0;
    return {
      id: meta.id,
      name: knowledgeName(meta.id, language.value),
      value,
      valueText: `${Math.round(value * 100)}%`,
      observed,
    };
  });
});

const masteryGroups = computed(() => {
  const levels: Array<{ key: string; labels: Record<string, string> }> = [
    { key: "beginner", labels: { "zh-CN": "入门", en: "Beginner", "zh-TW": "入門" } },
    { key: "intermediate", labels: { "zh-CN": "进阶", en: "Intermediate", "zh-TW": "進階" } },
    { key: "advanced", labels: { "zh-CN": "精通", en: "Advanced", "zh-TW": "精通" } },
  ];
  return levels.map((lv) => {
    const items = masteryItems.value.filter((m) => knowledgeLevel(m.id) === lv.key);
    const avg = items.length ? items.reduce((s, m) => s + m.value, 0) / items.length : 0;
    return {
      key: lv.key,
      label: () => lv.labels[language.value] ?? lv.labels["zh-CN"],
      items,
      avgText: `${Math.round(avg * 100)}%`,
    };
  });
});

const masterySummary = computed(() => {
  const observed = masteryItems.value.filter((m) => m.observed).length;
  if (observed === 0) return L.value.masterySummaryNone;
  return L.value.masterySummaryOne.replace("{n}", String(observed));
});

/* ---------- 生命周期 ---------- */

onShow(() => {
  if (!ensureAuthed()) return;
  user.value = uni.getStorageSync("user") || null;
  usernameDraft.value = user.value?.username || "";
  refreshProfile();
  loadMastery();
});

async function refreshProfile() {
  try {
    const profile = await getProfile();
    user.value = profile;
    uni.setStorageSync("user", profile);
    usernameDraft.value = profile.username;
  } catch {
    /* 统一错误处理 */
  }
}

async function loadMastery() {
  loadingMastery.value = true;
  try {
    mastery.value = await getMasteryRadar();
  } catch {
    mastery.value = null;
  } finally {
    loadingMastery.value = false;
  }
}

/* ---------- 账号设置 ---------- */

async function saveUsername() {
  const username = usernameDraft.value.trim();
  if (!username || username === user.value?.username) return;
  savingUsername.value = true;
  try {
    const updated = await updateProfile({ username });
    user.value = updated;
    uni.setStorageSync("user", updated);
    uni.showToast({ title: L.value.usernameUpdated, icon: "none" });
  } catch {
    /* 统一错误处理 */
  } finally {
    savingUsername.value = false;
  }
}

async function savePassword() {
  const f = pwdForm.value;
  if (!f.oldPassword || !f.newPassword || !f.confirmPassword) {
    uni.showToast({ title: L.value.passwordRequired, icon: "none" });
    return;
  }
  if (f.newPassword !== f.confirmPassword) {
    uni.showToast({ title: L.value.passwordMismatch, icon: "none" });
    return;
  }
  savingPwd.value = true;
  try {
    await changePassword({
      old_password: f.oldPassword,
      new_password: f.newPassword,
      confirm_password: f.confirmPassword,
    });
    uni.showToast({ title: L.value.passwordUpdated, icon: "none" });
    setTimeout(() => doLogout(), 900);
  } catch {
    /* 统一错误处理 */
  } finally {
    savingPwd.value = false;
  }
}

function confirmLogout() {
  uni.showModal({
    title: L.value.logout,
    content: L.value.logoutConfirm,
    success: (r) => {
      if (r.confirm) doLogout();
    },
  });
}

async function doLogout() {
  try {
    await logout();
  } catch {
    /* 无论成功与否都清理本地态 */
  }
  uni.removeStorageSync("token");
  uni.removeStorageSync("refresh_token");
  uni.removeStorageSync("user");
  uni.reLaunch({ url: "/pages/login/index" });
}

function confirmDeleteAccount() {
  if (!deletePassword.value) return;
  uni.showModal({
    title: L.value.deleteConfirm,
    content: L.value.deleteDesc,
    confirmText: L.value.deleteConfirm,
    confirmColor: "#E5484D",
    success: async (r) => {
      if (!r.confirm) return;
      deleting.value = true;
      try {
        await deleteAccount({ password: deletePassword.value });
        uni.removeStorageSync("token");
        uni.removeStorageSync("refresh_token");
        uni.removeStorageSync("user");
        uni.showToast({ title: "👋", icon: "none" });
        setTimeout(() => uni.reLaunch({ url: "/pages/login/index" }), 700);
      } catch {
        /* 统一错误处理 */
      } finally {
        deleting.value = false;
      }
    },
  });
}
</script>

<style lang="scss" scoped>
.profile-page {
  max-width: 700px;
  margin: 0 auto;
}

.mono {
  font-family: $font-mono;
}

/* ---------- 用户卡 ---------- */
.profile-hero {
  display: flex;
  align-items: center;
  gap: 26rpx;
  background: $brand-gradient;
  border-radius: 28rpx;
  padding: 40rpx 36rpx;
  margin: 40rpx 0 24rpx;
  box-shadow: 0 18rpx 44rpx rgba(76, 111, 255, 0.3);
}

.profile-avatar {
  width: 110rpx;
  height: 110rpx;
  border-radius: 32rpx;
  background: rgba(255, 255, 255, 0.22);
  border: 3rpx solid rgba(255, 255, 255, 0.45);
  display: flex;
  align-items: center;
  justify-content: center;

  .profile-avatar-text {
    font-size: 48rpx;
    font-weight: 800;
    color: #fff;
  }
}

.profile-info {
  flex: 1;
  min-width: 0;
}

.profile-name-row {
  display: flex;
  align-items: center;
  gap: 14rpx;

  .profile-name {
    font-size: 36rpx;
    font-weight: 800;
    color: #fff;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
}

.role-chip {
  padding: 4rpx 16rpx;
  border-radius: 999rpx;
  background: rgba(255, 255, 255, 0.22);
  color: #fff;
  font-size: 20rpx;
  font-weight: 600;
  flex-shrink: 0;

  &.teacher {
    background: rgba(245, 165, 36, 0.85);
    color: #fff;
  }
}

.profile-email {
  display: block;
  margin-top: 8rpx;
  font-size: 23rpx;
  color: rgba(255, 255, 255, 0.8);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ---------- 学习画像 ---------- */
.mastery-desc {
  display: block;
  font-size: 22rpx;
  color: $text-3;
  line-height: 1.65;
  margin: -6rpx 0 24rpx;
}

.mastery-group {
  margin-bottom: 28rpx;

  &:last-child {
    margin-bottom: 0;
  }
}

.mastery-group-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 14rpx;

  .mastery-group-title {
    font-size: 25rpx;
    font-weight: 700;
    color: $text-1;
  }

  .mastery-group-avg {
    font-size: 24rpx;
    font-weight: 700;
    color: $brand-deep;
  }
}

.mastery-row {
  display: flex;
  align-items: center;
  gap: 18rpx;
  margin-bottom: 16rpx;

  .mastery-name {
    width: 300rpx;
    flex-shrink: 0;
    font-size: 23rpx;
    color: $text-2;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;

    &.unobserved {
      color: #b6bece;
    }
  }

  .mastery-bar {
    flex: 1;
    height: 14rpx;
    border-radius: 999rpx;
    background: #eef1f8;
    overflow: hidden;
  }

  .mastery-fill {
    height: 100%;
    border-radius: 999rpx;
    background: $brand-gradient;
    transition: width 0.4s ease;

    &.unobserved {
      background: #ccd4e4;
    }
  }

  .mastery-value {
    width: 78rpx;
    flex-shrink: 0;
    text-align: right;
    font-size: 22rpx;
    font-weight: 700;
    color: $text-1;

    &.unobserved {
      color: #b6bece;
      font-weight: 500;
    }
  }
}

.loading-box {
  text-align: center;
  padding: 60rpx 0;
  color: $text-3;
  font-size: 24rpx;
}

/* ---------- 设置 ---------- */
.inline-row {
  display: flex;
  gap: 14rpx;

  .inline-input {
    flex: 1;
    min-width: 0;
  }
}

.collapse-toggle {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 20rpx 4rpx;
  font-size: 27rpx;
  color: $text-1;
  font-weight: 500;

  .collapse-arrow {
    color: $text-3;
    font-size: 22rpx;
  }
}

.pwd-form {
  padding-bottom: 8rpx;
}

.logout-btn {
  margin-top: 8rpx;
}

/* ---------- 危险区 ---------- */
.danger-zone {
  margin-top: 28rpx;
  background: #fff;
  border: 2rpx solid rgba(229, 72, 77, 0.16);
  border-radius: 22rpx;
  padding: 28rpx 30rpx;
  box-shadow: $shadow-card;

  .danger-zone-title {
    display: block;
    font-size: 26rpx;
    font-weight: 700;
    color: $danger;
  }

  .danger-zone-desc {
    display: block;
    margin-top: 8rpx;
    font-size: 22rpx;
    color: $text-3;
    line-height: 1.65;
  }

  .danger-zone-link {
    display: inline-block;
    margin-top: 18rpx;
    font-size: 24rpx;
    color: $danger;
    text-decoration: underline;
  }

  .delete-pwd-field {
    margin-top: 20rpx;
  }
}
</style>
