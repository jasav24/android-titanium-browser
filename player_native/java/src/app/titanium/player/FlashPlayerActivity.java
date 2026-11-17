package app.titanium.player;

import android.app.Activity;
import android.content.pm.ActivityInfo;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.Path;
import android.graphics.drawable.GradientDrawable;
import android.media.AudioManager;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.GestureDetector;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.MotionEvent;
import android.view.SurfaceView;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.SeekBar;
import android.widget.TextView;
import android.widget.Toast;

import androidx.media3.common.MediaItem;
import androidx.media3.common.PlaybackException;
import androidx.media3.common.PlaybackParameters;
import androidx.media3.common.Player;
import androidx.media3.common.VideoSize;
import androidx.media3.datasource.DefaultHttpDataSource;
import androidx.media3.exoplayer.ExoPlayer;
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory;

import java.util.Locale;

/**
 * Native fullscreen video player on ExoPlayer, with UC-browser-style gestures:
 * left/right vertical drags for brightness/volume, horizontal drag to seek,
 * double-tap to skip 10 s, long-press for 2x speed. All UI is built in code so
 * the fork carries no new resources.
 */
public class FlashPlayerActivity extends Activity {
    public static final String EXTRA_URL = "url";
    public static final String EXTRA_TITLE = "title";
    public static final String EXTRA_POSITION_MS = "position_ms";
    public static final String EXTRA_PAGE_URL = "page_url";

    private static final long HIDE_DELAY_MS = 3500;
    private static final long SEEK_SPAN_MS = 90_000;
    private static final float[] SPEEDS = {0.25f, 0.5f, 0.75f, 1f, 1.25f, 1.5f, 1.75f, 2f};

    private ExoPlayer mPlayer;
    private SurfaceView mSurfaceView;
    private FrameLayout mRoot;
    private View mTopBar;
    private View mBottomBar;
    private TextView mTitleView;
    private TextView mElapsedView;
    private TextView mTotalView;
    private TextView mHudView;
    private TextView mSpeedButton;
    private SeekBar mSeekBar;
    private ProgressBar mBuffering;
    private IconView mPlayPauseIcon;
    private IconView mLockIcon;
    private View mSheet;
    private View mScrim;

    private final Handler mHandler = new Handler(Looper.getMainLooper());
    private GestureDetector mGestureDetector;
    private AudioManager mAudioManager;

    private String mUrl;
    private String mTitle;
    private long mStartPositionMs;
    private float mSpeed = 1f;
    private boolean mLoop;
    private boolean mLocked;
    private boolean mControlsVisible = true;
    private boolean mSeekBarTracking;
    private boolean mWasPlayingOnPause;
    private boolean mSizeOriented;
    private boolean mRotateOverride;
    private boolean mRotated;
    private int mVideoWidth;
    private int mVideoHeight;

    private boolean mDragSeeking;
    private long mDragSeekTarget;
    private int mVolumeDragStart = -1;
    private float mBrightnessDragStart = -1f;
    private boolean mLongPressSpeed;
    private long mRippleUntil;
    private boolean mRippleRight;

    private final Runnable mHideControls = () -> setControlsVisible(false);
    private final Runnable mTicker =
            new Runnable() {
                @Override
                public void run() {
                    tick();
                    mHandler.postDelayed(this, 500);
                }
            };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        getWindow().setBackgroundDrawable(new android.graphics.drawable.ColorDrawable(Color.BLACK));
        mUrl = getIntent().getStringExtra(EXTRA_URL);
        mTitle = getIntent().getStringExtra(EXTRA_TITLE);
        mStartPositionMs = getIntent().getLongExtra(EXTRA_POSITION_MS, 0);
        String pageUrl = getIntent().getStringExtra(EXTRA_PAGE_URL);
        if (mUrl == null || mUrl.isEmpty()) {
            finish();
            return;
        }
        mAudioManager = (AudioManager) getSystemService(AUDIO_SERVICE);
        setVolumeControlStream(AudioManager.STREAM_MUSIC);
        buildUi();
        goImmersive();

        DefaultHttpDataSource.Factory httpFactory = new DefaultHttpDataSource.Factory();
        if (pageUrl != null && !pageUrl.isEmpty()) {
            java.util.Map<String, String> headers = new java.util.HashMap<>();
            headers.put("Referer", pageUrl);
            httpFactory.setDefaultRequestProperties(headers);
        }
        mPlayer =
                new ExoPlayer.Builder(this)
                        .setMediaSourceFactory(new DefaultMediaSourceFactory(httpFactory))
                        .setSeekBackIncrementMs(10_000)
                        .setSeekForwardIncrementMs(10_000)
                        .build();
        mPlayer.setVideoSurfaceView(mSurfaceView);
        mPlayer.addListener(
                new Player.Listener() {
                    @Override
                    public void onPlaybackStateChanged(int state) {
                        mBuffering.setVisibility(state == Player.STATE_BUFFERING ? View.VISIBLE : View.GONE);
                        if (state == Player.STATE_ENDED) setControlsVisible(true);
                        updatePlayPauseIcon();
                    }

                    @Override
                    public void onIsPlayingChanged(boolean isPlaying) {
                        updatePlayPauseIcon();
                        scheduleHide();
                    }

                    @Override
                    public void onVideoSizeChanged(VideoSize videoSize) {
                        mVideoWidth = videoSize.width;
                        mVideoHeight = videoSize.height;
                        layoutSurface();
                        maybeOrient();
                    }

                    @Override
                    public void onPlayerError(PlaybackException error) {
                        Toast.makeText(
                                        FlashPlayerActivity.this,
                                        "Cannot play this video (" + error.getErrorCodeName() + ")",
                                        Toast.LENGTH_LONG)
                                .show();
                        finish();
                    }
                });
        // DefaultMediaSourceFactory picks the HLS factory for .m3u8 (and by
        // sniffing) because media3-exoplayer-hls is on the classpath.
        mPlayer.setMediaItem(MediaItem.fromUri(mUrl), mStartPositionMs);
        mPlayer.prepare();
        mPlayer.setPlayWhenReady(true);
        mHandler.post(mTicker);
        scheduleHide();
    }

    // ---------------------------------------------------------------- UI

    private int dp(float value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }

    private void buildUi() {
        mRoot = new FrameLayout(this);
        mRoot.setBackgroundColor(Color.BLACK);
        mSurfaceView = new SurfaceView(this);
        mRoot.addView(
                mSurfaceView,
                new FrameLayout.LayoutParams(dp(320), dp(180), Gravity.CENTER));

        View gestureLayer = new GestureLayer(this);
        mRoot.addView(gestureLayer, matchParent());

        mHudView = new TextView(this);
        mHudView.setTextColor(Color.WHITE);
        mHudView.setTextSize(18);
        mHudView.setShadowLayer(6, 0, 2, Color.BLACK);
        mHudView.setBackgroundColor(0x99000000);
        mHudView.setPadding(dp(14), dp(8), dp(14), dp(8));
        mHudView.setVisibility(View.GONE);
        mRoot.addView(mHudView, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT,
                Gravity.CENTER));

        mBuffering = new ProgressBar(this);
        mBuffering.setVisibility(View.GONE);
        mRoot.addView(mBuffering, new FrameLayout.LayoutParams(dp(48), dp(48), Gravity.CENTER));

        buildTopBar();
        buildBottomBar();
        buildSheet();
        setContentView(mRoot);
        mRoot.addOnLayoutChangeListener((v, l, t, r, b, ol, ot, or, ob) -> layoutSurface());
    }

    private FrameLayout.LayoutParams matchParent() {
        return new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT);
    }

    private void buildTopBar() {
        LinearLayout bar = new LinearLayout(this);
        bar.setOrientation(LinearLayout.HORIZONTAL);
        bar.setGravity(Gravity.CENTER_VERTICAL);
        bar.setBackground(
                new GradientDrawable(
                        GradientDrawable.Orientation.TOP_BOTTOM,
                        new int[] {0xCC000000, 0x00000000}));
        bar.setPadding(dp(4), dp(4), dp(4), dp(12));

        IconView back = new IconView(this, "back");
        back.setOnClickListener(v -> finish());
        bar.addView(back, new LinearLayout.LayoutParams(dp(48), dp(48)));

        mTitleView = new TextView(this);
        mTitleView.setText(mTitle == null || mTitle.isEmpty() ? "Video" : mTitle);
        mTitleView.setTextColor(Color.WHITE);
        mTitleView.setTextSize(16);
        mTitleView.setSingleLine(true);
        mTitleView.setEllipsize(android.text.TextUtils.TruncateAt.END);
        LinearLayout.LayoutParams titleParams =
                new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
        titleParams.setMargins(dp(8), 0, dp(8), 0);
        bar.addView(mTitleView, titleParams);

        mLockIcon = new IconView(this, "unlock");
        mLockIcon.setOnClickListener(v -> toggleLock());
        bar.addView(mLockIcon, new LinearLayout.LayoutParams(dp(48), dp(48)));

        mTopBar = bar;
        mRoot.addView(bar, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT,
                Gravity.TOP));
    }

    private void buildBottomBar() {
        LinearLayout bar = new LinearLayout(this);
        bar.setOrientation(LinearLayout.VERTICAL);
        bar.setBackground(
                new GradientDrawable(
                        GradientDrawable.Orientation.BOTTOM_TOP,
                        new int[] {0xCC000000, 0x00000000}));
        bar.setPadding(dp(12), dp(12), dp(12), dp(6));

        LinearLayout timeRow = new LinearLayout(this);
        timeRow.setOrientation(LinearLayout.HORIZONTAL);
        timeRow.setGravity(Gravity.CENTER_VERTICAL);
        mElapsedView = makeTimeLabel("0:00");
        mTotalView = makeTimeLabel("0:00");
        mSeekBar = new SeekBar(this);
        mSeekBar.setMax(1000);
        mSeekBar.setOnSeekBarChangeListener(
                new SeekBar.OnSeekBarChangeListener() {
                    @Override
                    public void onProgressChanged(SeekBar seekBar, int progress, boolean fromUser) {
                        if (fromUser) {
                            long duration = mPlayer.getDuration();
                            if (duration > 0) {
                                showHud(formatTime(progress * duration / 1000)
                                        + " / " + formatTime(duration));
                            }
                        }
                    }

                    @Override
                    public void onStartTrackingTouch(SeekBar seekBar) {
                        mSeekBarTracking = true;
                        mHandler.removeCallbacks(mHideControls);
                    }

                    @Override
                    public void onStopTrackingTouch(SeekBar seekBar) {
                        mSeekBarTracking = false;
                        long duration = mPlayer.getDuration();
                        if (duration > 0) mPlayer.seekTo(seekBar.getProgress() * duration / 1000);
                        hideHud();
                        scheduleHide();
                    }
                });
        timeRow.addView(mElapsedView);
        timeRow.addView(mSeekBar, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        timeRow.addView(mTotalView);
        bar.addView(timeRow, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        LinearLayout actions = new LinearLayout(this);
        actions.setOrientation(LinearLayout.HORIZONTAL);
        actions.setGravity(Gravity.CENTER_VERTICAL);

        IconView rewind = new IconView(this, "rewind");
        rewind.setOnClickListener(v -> { seekBy(-10_000); showRipple(false); });
        actions.addView(rewind, new LinearLayout.LayoutParams(dp(48), dp(48)));

        mPlayPauseIcon = new IconView(this, "pause");
        mPlayPauseIcon.setOnClickListener(
                v -> {
                    if (mPlayer.isPlaying()) {
                        mPlayer.pause();
                    } else {
                        if (mPlayer.getPlaybackState() == Player.STATE_ENDED) mPlayer.seekTo(0);
                        mPlayer.play();
                    }
                    scheduleHide();
                });
        actions.addView(mPlayPauseIcon, new LinearLayout.LayoutParams(dp(56), dp(48)));

        IconView forward = new IconView(this, "forward");
        forward.setOnClickListener(v -> { seekBy(10_000); showRipple(true); });
        actions.addView(forward, new LinearLayout.LayoutParams(dp(48), dp(48)));

        actions.addView(new View(this), new LinearLayout.LayoutParams(0, 1, 1f));

        mSpeedButton = new TextView(this);
        mSpeedButton.setTextColor(Color.WHITE);
        mSpeedButton.setTextSize(14);
        mSpeedButton.setGravity(Gravity.CENTER);
        mSpeedButton.setText("1x");
        mSpeedButton.setOnClickListener(v -> cycleSpeed());
        actions.addView(mSpeedButton, new LinearLayout.LayoutParams(dp(56), dp(48)));

        IconView settings = new IconView(this, "settings");
        settings.setOnClickListener(v -> showSheet(true));
        actions.addView(settings, new LinearLayout.LayoutParams(dp(48), dp(48)));

        bar.addView(actions, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, dp(48)));

        mBottomBar = bar;
        mRoot.addView(bar, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT,
                Gravity.BOTTOM));
    }

    private TextView makeTimeLabel(String text) {
        TextView view = new TextView(this);
        view.setText(text);
        view.setTextColor(Color.WHITE);
        view.setTextSize(12);
        view.setPadding(dp(6), 0, dp(6), 0);
        return view;
    }

    private void buildSheet() {
        mScrim = new View(this);
        mScrim.setBackgroundColor(0x66000000);
        mScrim.setVisibility(View.GONE);
        mScrim.setOnClickListener(v -> showSheet(false));
        mRoot.addView(mScrim, matchParent());

        LinearLayout sheet = new LinearLayout(this);
        sheet.setOrientation(LinearLayout.VERTICAL);
        GradientDrawable background = new GradientDrawable();
        background.setColor(0xFF212121);
        background.setCornerRadii(new float[] {
                dp(12), dp(12), dp(12), dp(12), 0, 0, 0, 0});
        sheet.setBackground(background);
        sheet.setPadding(dp(16), dp(12), dp(16), dp(16));
        sheet.setClickable(true);

        TextView speedLabel = makeSheetLabel("Playback speed");
        sheet.addView(speedLabel);
        LinearLayout chips = new LinearLayout(this);
        chips.setOrientation(LinearLayout.HORIZONTAL);
        for (float speed : SPEEDS) {
            TextView chip = new TextView(this);
            chip.setText(speed == (int) speed ? ((int) speed) + "x" : speed + "x");
            chip.setTextColor(Color.WHITE);
            chip.setTextSize(13);
            chip.setGravity(Gravity.CENTER);
            chip.setTag(speed);
            chip.setOnClickListener(
                    v -> {
                        setSpeed((Float) v.getTag());
                        showSheet(false);
                    });
            LinearLayout.LayoutParams params =
                    new LinearLayout.LayoutParams(0, dp(36), 1f);
            params.setMargins(dp(2), 0, dp(2), 0);
            chips.addView(chip, params);
        }
        sheet.addView(chips, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        sheet.addView(makeSheetLabel("Options"));
        LinearLayout loopRow = makeSheetRow("Loop video", "Off");
        loopRow.setOnClickListener(
                v -> {
                    mLoop = !mLoop;
                    mPlayer.setRepeatMode(mLoop ? Player.REPEAT_MODE_ONE : Player.REPEAT_MODE_OFF);
                    ((TextView) ((LinearLayout) v).getChildAt(1)).setText(mLoop ? "On" : "Off");
                });
        sheet.addView(loopRow);
        LinearLayout rotateRow = makeSheetRow("Rotate screen", "Off");
        rotateRow.setOnClickListener(
                v -> {
                    mRotateOverride = true;
                    mRotated = !mRotated;
                    applyOrientation();
                    ((TextView) ((LinearLayout) v).getChildAt(1)).setText(mRotated ? "On" : "Off");
                });
        sheet.addView(rotateRow);

        mSheet = sheet;
        mSheet.setVisibility(View.GONE);
        mRoot.addView(mSheet, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT,
                Gravity.BOTTOM));
    }

    private TextView makeSheetLabel(String text) {
        TextView view = new TextView(this);
        view.setText(text);
        view.setTextColor(0xFFBDBDBD);
        view.setTextSize(12);
        view.setPadding(0, dp(8), 0, dp(8));
        return view;
    }

    private LinearLayout makeSheetRow(String title, String value) {
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setPadding(0, dp(10), 0, dp(10));
        TextView titleView = new TextView(this);
        titleView.setText(title);
        titleView.setTextColor(Color.WHITE);
        titleView.setTextSize(14);
        row.addView(titleView, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        TextView valueView = new TextView(this);
        valueView.setText(value);
        valueView.setTextColor(0xFFBDBDBD);
        valueView.setTextSize(14);
        row.addView(valueView);
        return row;
    }

    private void showSheet(boolean show) {
        mSheet.setVisibility(show ? View.VISIBLE : View.GONE);
        mScrim.setVisibility(show ? View.VISIBLE : View.GONE);
        if (show) {
            mHandler.removeCallbacks(mHideControls);
            refreshSpeedChips();
        } else {
            scheduleHide();
        }
    }

    private void refreshSpeedChips() {
        LinearLayout chips = (LinearLayout) ((LinearLayout) mSheet).getChildAt(1);
        for (int i = 0; i < chips.getChildCount(); i++) {
            TextView chip = (TextView) chips.getChildAt(i);
            boolean active = (Float) chip.getTag() == mSpeed;
            chip.setBackgroundColor(active ? 0xFFFF0000 : 0xFF333333);
        }
    }

    // ------------------------------------------------------------ gestures

    private class GestureLayer extends View {
        GestureLayer(android.content.Context context) {
            super(context);
            mGestureDetector = new GestureDetector(context, new GestureListener(this));
            mGestureDetector.setIsLongpressEnabled(true);
        }

        @Override
        public boolean onTouchEvent(MotionEvent event) {
            mGestureDetector.onTouchEvent(event);
            switch (event.getActionMasked()) {
                case MotionEvent.ACTION_DOWN:
                    mDragSeeking = false;
                    mVolumeDragStart = -1;
                    mBrightnessDragStart = -1f;
                    break;
                case MotionEvent.ACTION_UP:
                case MotionEvent.ACTION_CANCEL:
                    if (mDragSeeking) {
                        mPlayer.seekTo(mDragSeekTarget);
                        mDragSeeking = false;
                        hideHud();
                    }
                    if (mLongPressSpeed) {
                        mLongPressSpeed = false;
                        applySpeed();
                        hideHud();
                    }
                    mVolumeDragStart = -1;
                    mBrightnessDragStart = -1f;
                    scheduleHide();
                    break;
            }
            return true;
        }

        @Override
        protected void onDraw(Canvas canvas) {
            if (System.currentTimeMillis() < mRippleUntil) {
                Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
                paint.setColor(0x33FFFFFF);
                float radius = getHeight() * 0.45f;
                float cx = mRippleRight ? getWidth() : 0;
                canvas.drawCircle(cx, getHeight() / 2f, radius, paint);
                paint.setColor(Color.WHITE);
                paint.setTextSize(dp(16));
                paint.setTextAlign(mRippleRight ? Paint.Align.RIGHT : Paint.Align.LEFT);
                float tx = mRippleRight ? getWidth() - dp(40) : dp(40);
                canvas.drawText(mRippleRight ? "+10 s" : "-10 s", tx,
                        getHeight() / 2f + dp(6), paint);
                postInvalidateOnAnimation();
            }
        }
    }

    private class GestureListener extends GestureDetector.SimpleOnGestureListener {
        private final View mView;

        GestureListener(View view) {
            mView = view;
        }

        @Override
        public boolean onSingleTapConfirmed(MotionEvent e) {
            if (mLocked) {
                mLockIcon.setVisibility(View.VISIBLE);
                mHandler.postDelayed(() -> { if (mLocked) mLockIcon.setVisibility(View.GONE); },
                        HIDE_DELAY_MS);
                return true;
            }
            setControlsVisible(!mControlsVisible);
            return true;
        }

        @Override
        public boolean onDoubleTap(MotionEvent e) {
            if (mLocked) return true;
            boolean right = e.getX() > mView.getWidth() / 2f;
            seekBy(right ? 10_000 : -10_000);
            showRipple(right);
            return true;
        }

        @Override
        public void onLongPress(MotionEvent e) {
            if (mLocked || mPlayer == null) return;
            mLongPressSpeed = true;
            mPlayer.setPlaybackParameters(new PlaybackParameters(2f));
            showHud("2x faster");
        }

        @Override
        public boolean onScroll(MotionEvent e1, MotionEvent e2, float dx, float dy) {
            if (mLocked || e1 == null) return true;
            float totalX = e2.getX() - e1.getX();
            float totalY = e2.getY() - e1.getY();
            boolean horizontal = Math.abs(totalX) > Math.abs(totalY);
            if (horizontal) {
                long duration = mPlayer.getDuration();
                if (duration <= 0) return true;
                if (!mDragSeeking) {
                    mDragSeeking = true;
                    mDragSeekTarget = mPlayer.getCurrentPosition();
                    mHandler.removeCallbacks(mHideControls);
                }
                long delta = (long) (totalX / mView.getWidth() * SEEK_SPAN_MS);
                mDragSeekTarget = clamp(mPlayer.getCurrentPosition() + delta, 0, duration);
                showHud(formatTime(mDragSeekTarget) + " / " + formatTime(duration));
                return true;
            }
            float zone = e1.getX() / mView.getWidth();
            float fraction = -totalY / mView.getHeight();
            if (zone < 0.25f) {
                if (mBrightnessDragStart < 0) {
                    mBrightnessDragStart = getWindow().getAttributes().screenBrightness;
                    if (mBrightnessDragStart < 0) mBrightnessDragStart = 0.5f;
                }
                float brightness = clampF(mBrightnessDragStart + fraction, 0.01f, 1f);
                WindowManager.LayoutParams attrs = getWindow().getAttributes();
                attrs.screenBrightness = brightness;
                getWindow().setAttributes(attrs);
                showHud("Brightness " + Math.round(brightness * 100) + "%");
            } else if (zone > 0.75f) {
                if (mVolumeDragStart < 0) {
                    mVolumeDragStart = mAudioManager.getStreamVolume(AudioManager.STREAM_MUSIC);
                }
                int max = mAudioManager.getStreamMaxVolume(AudioManager.STREAM_MUSIC);
                int volume = (int) clamp(mVolumeDragStart + Math.round(fraction * max), 0, max);
                mAudioManager.setStreamVolume(AudioManager.STREAM_MUSIC, volume, 0);
                showHud("Volume " + (max == 0 ? 0 : volume * 100 / max) + "%");
            }
            return true;
        }
    }

    private void showRipple(boolean right) {
        mRippleRight = right;
        mRippleUntil = System.currentTimeMillis() + 400;
        for (int i = 0; i < mRoot.getChildCount(); i++) {
            View child = mRoot.getChildAt(i);
            if (child instanceof GestureLayer) child.invalidate();
        }
    }

    // ------------------------------------------------------------ playback

    private void seekBy(long deltaMs) {
        long duration = mPlayer.getDuration();
        long target = mPlayer.getCurrentPosition() + deltaMs;
        if (duration > 0) target = clamp(target, 0, duration);
        mPlayer.seekTo(Math.max(0, target));
        scheduleHide();
    }

    private void cycleSpeed() {
        int index = 0;
        for (int i = 0; i < SPEEDS.length; i++) {
            if (SPEEDS[i] == mSpeed) {
                index = (i + 1) % SPEEDS.length;
                break;
            }
        }
        setSpeed(SPEEDS[index]);
    }

    private void setSpeed(float speed) {
        mSpeed = speed;
        applySpeed();
        mSpeedButton.setText(speed == (int) speed ? ((int) speed) + "x" : speed + "x");
        scheduleHide();
    }

    private void applySpeed() {
        mPlayer.setPlaybackParameters(new PlaybackParameters(mLongPressSpeed ? 2f : mSpeed));
    }

    private void updatePlayPauseIcon() {
        if (mPlayPauseIcon != null) {
            mPlayPauseIcon.setIcon(mPlayer.isPlaying() ? "pause" : "play");
        }
    }

    private void tick() {
        if (mPlayer == null) return;
        long position = mPlayer.getCurrentPosition();
        long duration = mPlayer.getDuration();
        mElapsedView.setText(formatTime(mDragSeeking ? mDragSeekTarget : position));
        mTotalView.setText(duration > 0 ? formatTime(duration) : "0:00");
        if (!mSeekBarTracking && !mDragSeeking && duration > 0) {
            mSeekBar.setProgress((int) (position * 1000 / duration));
        }
    }

    private static String formatTime(long ms) {
        long totalSeconds = Math.max(0, ms / 1000);
        long hours = totalSeconds / 3600;
        long minutes = (totalSeconds % 3600) / 60;
        long seconds = totalSeconds % 60;
        if (hours > 0) {
            return String.format(Locale.US, "%d:%02d:%02d", hours, minutes, seconds);
        }
        return String.format(Locale.US, "%d:%02d", minutes, seconds);
    }

    private static long clamp(long value, long min, long max) {
        return Math.max(min, Math.min(max, value));
    }

    private static float clampF(float value, float min, float max) {
        return Math.max(min, Math.min(max, value));
    }

    // ------------------------------------------------------------- chrome

    private void setControlsVisible(boolean visible) {
        mControlsVisible = visible;
        mTopBar.setVisibility(visible ? View.VISIBLE : View.GONE);
        mBottomBar.setVisibility(visible ? View.VISIBLE : View.GONE);
        if (visible) {
            scheduleHide();
        } else {
            mHandler.removeCallbacks(mHideControls);
            showSheet(false);
        }
    }

    private void scheduleHide() {
        mHandler.removeCallbacks(mHideControls);
        if (mPlayer != null && mPlayer.isPlaying() && !mSeekBarTracking) {
            mHandler.postDelayed(mHideControls, HIDE_DELAY_MS);
        }
    }

    private void toggleLock() {
        mLocked = !mLocked;
        mLockIcon.setIcon(mLocked ? "lock" : "unlock");
        if (mLocked) {
            setControlsVisible(true);
            mBottomBar.setVisibility(View.GONE);
            mHandler.removeCallbacks(mHideControls);
            mHandler.postDelayed(
                    () -> {
                        if (mLocked) {
                            mTopBar.setVisibility(View.GONE);
                            mLockIcon.setVisibility(View.GONE);
                        }
                    },
                    HIDE_DELAY_MS);
        } else {
            mLockIcon.setVisibility(View.VISIBLE);
            setControlsVisible(true);
        }
    }

    private void showHud(String text) {
        mHudView.setText(text);
        mHudView.setVisibility(View.VISIBLE);
    }

    private void hideHud() {
        mHudView.setVisibility(View.GONE);
    }

    private void layoutSurface() {
        if (mVideoWidth <= 0 || mVideoHeight <= 0) return;
        int width = mRoot.getWidth();
        int height = mRoot.getHeight();
        if (width == 0 || height == 0) return;
        float videoRatio = (float) mVideoWidth / mVideoHeight;
        int w = width;
        int h = Math.round(w / videoRatio);
        if (h > height) {
            h = height;
            w = Math.round(h * videoRatio);
        }
        ViewGroup.LayoutParams params = mSurfaceView.getLayoutParams();
        params.width = w;
        params.height = h;
        mSurfaceView.setLayoutParams(params);
    }

    private void maybeOrient() {
        if (mSizeOriented || mVideoWidth <= 0 || mVideoHeight <= 0) return;
        mSizeOriented = true;
        mRotated = false;
        applyOrientation();
    }

    private void applyOrientation() {
        boolean wide = mVideoWidth >= mVideoHeight;
        boolean landscape = mRotateOverride ? mRotated : wide;
        setRequestedOrientation(
                landscape
                        ? ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE
                        : ActivityInfo.SCREEN_ORIENTATION_SENSOR_PORTRAIT);
    }

    private void goImmersive() {
        getWindow().getDecorView().setSystemUiVisibility(
                View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                        | View.SYSTEM_UI_FLAG_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                        | View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                        | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION);
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) goImmersive();
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        switch (keyCode) {
            case KeyEvent.KEYCODE_SPACE:
            case KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE:
                if (mPlayer.isPlaying()) mPlayer.pause(); else mPlayer.play();
                return true;
            case KeyEvent.KEYCODE_DPAD_LEFT:
                seekBy(-10_000);
                return true;
            case KeyEvent.KEYCODE_DPAD_RIGHT:
                seekBy(10_000);
                return true;
        }
        return super.onKeyDown(keyCode, event);
    }

    // ---------------------------------------------------------- lifecycle

    @Override
    protected void onResume() {
        super.onResume();
        goImmersive();
        if (mPlayer != null && mWasPlayingOnPause) mPlayer.play();
    }

    @Override
    protected void onPause() {
        super.onPause();
        if (mPlayer != null) {
            mWasPlayingOnPause = mPlayer.isPlaying();
            mPlayer.pause();
        }
    }

    @Override
    protected void onStop() {
        super.onStop();
        if (mPlayer != null) mPlayer.pause();
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        mHandler.removeCallbacksAndMessages(null);
        if (mPlayer != null) {
            mPlayer.release();
            mPlayer = null;
        }
        WindowManager.LayoutParams attrs = getWindow().getAttributes();
        attrs.screenBrightness = WindowManager.LayoutParams.BRIGHTNESS_OVERRIDE_NONE;
        getWindow().setAttributes(attrs);
    }

    // -------------------------------------------------------------- icons

    /** Plain white line icons drawn with paths, so no drawable resources are needed. */
    private class IconView extends View {
        private String mIcon;
        private final Paint mPaint = new Paint(Paint.ANTI_ALIAS_FLAG);

        IconView(android.content.Context context, String icon) {
            super(context);
            mIcon = icon;
            mPaint.setColor(Color.WHITE);
            mPaint.setStyle(Paint.Style.STROKE);
            mPaint.setStrokeWidth(dp(2));
            mPaint.setStrokeCap(Paint.Cap.ROUND);
            mPaint.setStrokeJoin(Paint.Join.ROUND);
            setClickable(true);
        }

        void setIcon(String icon) {
            mIcon = icon;
            invalidate();
        }

        @Override
        protected void onDraw(Canvas canvas) {
            float s = Math.min(getWidth(), getHeight()) / 24f;
            canvas.save();
            canvas.translate((getWidth() - 24 * s) / 2f, (getHeight() - 24 * s) / 2f);
            canvas.scale(s, s);
            Path path = new Path();
            Paint fill = new Paint(mPaint);
            fill.setStyle(Paint.Style.FILL);
            switch (mIcon) {
                case "back":
                    path.moveTo(15, 4);
                    path.lineTo(7, 12);
                    path.lineTo(15, 20);
                    canvas.drawPath(path, mPaint);
                    break;
                case "play":
                    path.moveTo(8, 5);
                    path.lineTo(19, 12);
                    path.lineTo(8, 19);
                    path.close();
                    canvas.drawPath(path, fill);
                    break;
                case "pause":
                    canvas.drawRect(7, 5, 10, 19, fill);
                    canvas.drawRect(14, 5, 17, 19, fill);
                    break;
                case "rewind":
                    path.moveTo(12, 6);
                    path.lineTo(5, 12);
                    path.lineTo(12, 18);
                    path.moveTo(19, 6);
                    path.lineTo(12, 12);
                    path.lineTo(19, 18);
                    canvas.drawPath(path, mPaint);
                    break;
                case "forward":
                    path.moveTo(5, 6);
                    path.lineTo(12, 12);
                    path.lineTo(5, 18);
                    path.moveTo(12, 6);
                    path.lineTo(19, 12);
                    path.lineTo(12, 18);
                    canvas.drawPath(path, mPaint);
                    break;
                case "lock":
                case "unlock":
                    canvas.drawRoundRect(6, 11, 18, 20, 2, 2, mPaint);
                    path.moveTo(8, 11);
                    path.lineTo(8, 8);
                    path.arcTo(new android.graphics.RectF(8, 4, 16, 12), 180, 180);
                    if ("lock".equals(mIcon)) path.lineTo(16, 11);
                    canvas.drawPath(path, mPaint);
                    break;
                case "settings":
                    canvas.drawCircle(12, 12, 3.5f, mPaint);
                    for (int i = 0; i < 8; i++) {
                        double angle = i * Math.PI / 4;
                        float x1 = 12 + (float) Math.cos(angle) * 6.5f;
                        float y1 = 12 + (float) Math.sin(angle) * 6.5f;
                        float x2 = 12 + (float) Math.cos(angle) * 9f;
                        float y2 = 12 + (float) Math.sin(angle) * 9f;
                        canvas.drawLine(x1, y1, x2, y2, mPaint);
                    }
                    break;
            }
            canvas.restore();
        }
    }
}