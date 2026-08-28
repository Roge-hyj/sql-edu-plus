<template>
  <view class="empty">
    <view class="empty-icon">{{ icon }}</view>
    <text class="empty-title">{{ title }}</text>
    <text v-if="desc" class="empty-desc">{{ desc }}</text>
  </view>
</template>

<script setup lang="ts">
withDefaults(
  defineProps<{
    icon?: string;
    title: string;
    desc?: string;
  }>(),
  { icon: "🗂️" },
);
</script>

<style lang="scss" scoped>
.empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 80rpx 40rpx;
}

.empty-icon {
  font-size: 72rpx;
  margin-bottom: 20rpx;
  opacity: 0.85;
}

.empty-title {
  font-size: 28rpx;
  font-weight: 600;
  color: $text-2;
}

.empty-desc {
  margin-top: 10rpx;
  font-size: 24rpx;
  color: $text-3;
  text-align: center;
}
</style>
