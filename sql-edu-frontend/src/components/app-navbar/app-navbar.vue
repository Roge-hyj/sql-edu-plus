<template>
  <view class="navbar" :style="{ paddingTop: statusBarHeight + 'px' }">
    <view class="navbar-inner">
      <view v-if="showBack" class="nav-back" @tap="goBack">
        <text class="nav-back-icon">‹</text>
      </view>
      <view class="nav-title-box">
        <text class="nav-title">{{ title }}</text>
      </view>
      <view class="nav-right">
        <slot name="right" />
      </view>
    </view>
  </view>
  <view :style="{ height: statusBarHeight + 44 + 'px' }" />
</template>

<script setup lang="ts">
import { ref } from "vue";

defineProps<{
  title: string;
  showBack?: boolean;
}>();

const statusBarHeight = ref(0);
try {
  const info = uni.getWindowInfo ? uni.getWindowInfo() : uni.getSystemInfoSync();
  statusBarHeight.value = info.statusBarHeight || 0;
} catch {
  statusBarHeight.value = 0;
}

function goBack() {
  const pages = getCurrentPages();
  if (pages.length > 1) {
    uni.navigateBack();
  } else {
    uni.reLaunch({ url: "/pages/index/index" });
  }
}
</script>

<style lang="scss" scoped>
.navbar {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  z-index: 100;
  background: rgba(244, 246, 251, 0.86);
  backdrop-filter: blur(16px);
  -webkit-backdrop-filter: blur(16px);
}

.navbar-inner {
  height: 44px;
  display: flex;
  align-items: center;
  padding: 0 24rpx;
  gap: 12rpx;
  max-width: 700px;
  margin: 0 auto;
}

.nav-back {
  width: 60rpx;
  height: 60rpx;
  border-radius: 50%;
  background: #fff;
  box-shadow: 0 4rpx 14rpx rgba(23, 29, 43, 0.08);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;

  .nav-back-icon {
    font-size: 40rpx;
    line-height: 1;
    color: $text-1;
    margin-top: -4rpx;
  }
}

.nav-title-box {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}

.nav-title {
  font-size: 32rpx;
  font-weight: 700;
  color: $text-1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.nav-right {
  display: flex;
  align-items: center;
  gap: 14rpx;
  flex-shrink: 0;
}
</style>
