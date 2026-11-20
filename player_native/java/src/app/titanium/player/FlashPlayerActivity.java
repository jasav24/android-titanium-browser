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
    private View mCenterControls;
    private TextView mHudView;
    private TextView mSpeedButton;
    private TextView mTimeView;
    private SeekLine mSeekBar;
    private ProgressBar mBuffering;
    private IconView mPlayPauseIcon;
    private IconView mLockIcon;
    private TextView mSubtitleView;
    private IconView mCcButton;
    private boolean mSubtitlesEnabled;
    private boolean mHasTextTrack;
    private View mSheet;
    private View mScrim;
    private LinearLayout mSubSheet;
    private TextView mSpeedValue;
    private TextView mQualityValue;
    private int mQualityHeight = -1; // -1 = Auto
    private String[] mQualityOptions = {"Auto"};

    private final Handler mHandler = new Handler(Looper.getMainLooper());
    private GestureDetector mGestureDetector;
    private AudioManager mAudioManager;

    private String mUrl;
    private String mTitle;
    private String mHost;
    private long mStartPositionMs;
    private float mSpeed = 1f;
    private boolean mLoop;
    private boolean mLocked;
    private boolean mFitWide;
    private int mAspectMode; // 0 fit, 1 cover, 2 stretch
    private boolean mBackgroundPlay;
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
        try {
            mHost = pageUrl == null || pageUrl.isEmpty()
                    ? ""
                    : android.net.Uri.parse(pageUrl).getHost();
        } catch (Exception e) {
            mHost = "";
        }
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
                    public void onTracksChanged(androidx.media3.common.Tracks tracks) {
                        boolean has = false;
                        try {
                            for (androidx.media3.common.Tracks.Group group : tracks.getGroups()) {
                                if (group.isSupported()
                                        && group.getType()
                                                == androidx.media3.common.C.TRACK_TYPE_TEXT) {
                                    has = true;
                                    break;
                                }
                            }
                        } catch (Exception e) {
                            // Best-effort; keep previous state.
                            return;
                        }
                        mHasTextTrack = has;
                        if (mCcButton != null) {
                            mCcButton.setVisibility(has ? View.VISIBLE : View.GONE);
                        }
                        if (!has) {
                            mSubtitlesEnabled = false;
                            mSubtitleView.setVisibility(View.GONE);
                        }
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
        mHudView.setTextSize(14);
        mHudView.setShadowLayer(6, 0, 2, Color.BLACK);
        mHudView.setBackgroundColor(0x99000000);
        mHudView.setPadding(dp(10), dp(5), dp(10), dp(5));
        mHudView.setVisibility(View.GONE);
        mRoot.addView(mHudView, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT,
                Gravity.CENTER));

        mBuffering = new ProgressBar(this);
        mBuffering.setVisibility(View.GONE);
        mRoot.addView(mBuffering, new FrameLayout.LayoutParams(dp(36), dp(36), Gravity.CENTER));

        buildTopBar();
        buildCenterControls();
        buildBottomBar();
        buildSubtitleAndLock();
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
                        new int[] {0xB3000000, 0x00000000}));
        bar.setPadding(dp(6), dp(4), dp(6), dp(18));

        IconView back = new IconView(this, "chevron");
        back.setOnClickListener(v -> finish());
        bar.addView(back, new LinearLayout.LayoutParams(dp(36), dp(36)));

        LinearLayout titles = new LinearLayout(this);
        titles.setOrientation(LinearLayout.VERTICAL);
        titles.setGravity(Gravity.CENTER_VERTICAL);
        titles.setPadding(dp(6), 0, dp(6), 0);
        TextView title = new TextView(this);
        title.setText(mTitle == null || mTitle.isEmpty() ? "Video" : mTitle);
        title.setTextColor(Color.WHITE);
        title.setTextSize(15);
        title.setTypeface(android.graphics.Typeface.DEFAULT_BOLD);
        title.setSingleLine(true);
        title.setEllipsize(android.text.TextUtils.TruncateAt.END);
        titles.addView(title);
        TextView subtitle = new TextView(this);
        subtitle.setText(mHost == null ? "" : mHost);
        subtitle.setTextColor(0xB3FFFFFF);
        subtitle.setTextSize(11);
        subtitle.setSingleLine(true);
        titles.addView(subtitle);
        bar.addView(
                titles,
                new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f));

        mCcButton = new IconView(this, "cc");
        mCcButton.setVisibility(View.GONE);
        mCcButton.setOnClickListener(v -> toggleSubtitles());
        bar.addView(mCcButton, iconParams());

        IconView gear = new IconView(this, "settings");
        gear.setOnClickListener(
                v -> {
                    mAspectMode = (mAspectMode + 1) % 3;
                    mFitWide = mAspectMode == 1;
                    layoutSurface();
                    Toast.makeText(
                                    this,
                                    new String[] {"Fit", "Cover", "Stretch"}[mAspectMode]
                                            + " aspect",
                                    Toast.LENGTH_SHORT)
                            .show();
                    scheduleHide();
                });
        bar.addView(gear, iconParams());

        IconView headphones = new IconView(this, "headphones");
        headphones.setOnClickListener(
                v -> {
                    mBackgroundPlay = !mBackgroundPlay;
                    Toast.makeText(
                                    this,
                                    "Background play " + (mBackgroundPlay ? "on" : "off"),
                                    Toast.LENGTH_SHORT)
                            .show();
                    scheduleHide();
                });
        bar.addView(headphones, iconParams());

        IconView overflow = new IconView(this, "dots");
        overflow.setOnClickListener(v -> showSheet(true));
        bar.addView(overflow, iconParams());

        mTopBar = bar;
        mRoot.addView(bar, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT,
                Gravity.TOP));
    }

    private LinearLayout.LayoutParams iconParams() {
        LinearLayout.LayoutParams params =
                new LinearLayout.LayoutParams(dp(36), dp(36));
        params.setMargins(dp(2), 0, dp(2), 0);
        return params;
    }

    private void buildSubtitleAndLock() {
        mSubtitleView = new TextView(this);
        mSubtitleView.setTextColor(Color.WHITE);
        mSubtitleView.setTextSize(15);
        mSubtitleView.setGravity(Gravity.CENTER);
        mSubtitleView.setBackgroundColor(0x99000000);
        mSubtitleView.setPadding(dp(10), dp(4), dp(10), dp(4));
        mSubtitleView.setVisibility(View.GONE);
        FrameLayout.LayoutParams subtitleParams =
                new FrameLayout.LayoutParams(
                        ViewGroup.LayoutParams.WRAP_CONTENT,
                        ViewGroup.LayoutParams.WRAP_CONTENT,
                        Gravity.BOTTOM | Gravity.CENTER_HORIZONTAL);
        subtitleParams.bottomMargin = dp(64);
        mRoot.addView(mSubtitleView, subtitleParams);

        // The reference top bar has no lock glyph, so the lock lives in the
        // sheet; this overlay only appears while locked, on tap, to unlock.
        mLockIcon = new IconView(this, "lock");
        mLockIcon.setOnClickListener(v -> toggleLock());
        mLockIcon.setVisibility(View.GONE);
        FrameLayout.LayoutParams lockParams =
                new FrameLayout.LayoutParams(dp(36), dp(36), Gravity.TOP | Gravity.END);
        lockParams.setMargins(0, dp(6), dp(6), 0);
        mRoot.addView(mLockIcon, lockParams);
    }

    private void toggleSubtitles() {
        if (!mHasTextTrack) {
            Toast.makeText(this, "This video has no subtitle track", Toast.LENGTH_SHORT).show();
            return;
        }
        mSubtitlesEnabled = !mSubtitlesEnabled;
        if (!mSubtitlesEnabled) {
            mSubtitleView.setVisibility(View.GONE);
        }
        Toast.makeText(
                        this,
                        "Subtitles " + (mSubtitlesEnabled ? "on" : "off"),
                        Toast.LENGTH_SHORT)
                .show();
        scheduleHide();
    }

    private IconView circle(String icon, int size, View.OnClickListener onClick) {
        IconView view = new IconView(this, icon);
        view.setOnClickListener(onClick);
        return view;
    }

    private void buildCenterControls() {
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER);

        IconView prev = circle("prev", dp(52), v -> { seekBy(-10_000); showRipple(false); });
        row.addView(prev, new LinearLayout.LayoutParams(dp(52), dp(52)));

        mPlayPauseIcon =
                circle(
                        "pause",
                        dp(72),
                        v -> {
                            if (mPlayer.isPlaying()) {
                                mPlayer.pause();
                            } else {
                                if (mPlayer.getPlaybackState() == Player.STATE_ENDED) {
                                    mPlayer.seekTo(0);
                                }
                                mPlayer.play();
                            }
                            scheduleHide();
                        });
        LinearLayout.LayoutParams centerParams =
                new LinearLayout.LayoutParams(dp(72), dp(72));
        centerParams.setMargins(dp(14), 0, dp(14), 0);
        row.addView(mPlayPauseIcon, centerParams);

        IconView next = circle("next", dp(52), v -> { seekBy(10_000); showRipple(true); });
        row.addView(next, new LinearLayout.LayoutParams(dp(52), dp(52)));

        mCenterControls = row;
        mRoot.addView(row, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT,
                Gravity.CENTER));
    }

    private void buildBottomBar() {
        LinearLayout bar = new LinearLayout(this);
        bar.setOrientation(LinearLayout.HORIZONTAL);
        bar.setGravity(Gravity.CENTER_VERTICAL);
        bar.setPadding(dp(10), 0, dp(6), dp(4));

        mTimeView = new TextView(this);
        mTimeView.setTextColor(Color.WHITE);
        mTimeView.setTextSize(13);
        mTimeView.setText("0:00 / 0:00");
        mTimeView.setShadowLayer(4, 0, 1, Color.BLACK);
        mTimeView.setGravity(Gravity.CENTER_VERTICAL);
        mTimeView.setIncludeFontPadding(false);
        mTimeView.setPadding(dp(2), 0, dp(2), 0);
        bar.addView(mTimeView, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, dp(30)));

        mSeekBar = new SeekLine(this);
        LinearLayout.LayoutParams seekParams =
                new LinearLayout.LayoutParams(0, dp(30), 1f);
        seekParams.setMargins(dp(10), 0, dp(10), 0);
        bar.addView(mSeekBar, seekParams);

        IconView expand = new IconView(this, "expand");
        expand.setOnClickListener(
                v -> {
                    mAspectMode = mAspectMode == 0 ? 1 : 0;
                    mFitWide = mAspectMode == 1;
                    layoutSurface();
                    scheduleHide();
                });
        bar.addView(expand, new LinearLayout.LayoutParams(dp(30), dp(30)));

        mBottomBar = bar;
        mRoot.addView(bar, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT,
                Gravity.BOTTOM));
    }

    /** Thin ReVanced-style progress line: red gradient, pink thumb, dashed rest. */
    private class SeekLine extends View {
        private long mDuration;
        private long mPosition;
        private long mBufferedMs;
        private boolean mDragging;
        private long mDragTargetMs;

        SeekLine(android.content.Context context) {
            super(context);
        }

        void setProgress(long positionMs, long durationMs, long bufferedMs) {
            mPosition = positionMs;
            mDuration = durationMs;
            mBufferedMs = bufferedMs;
            if (!mDragging) invalidate();
        }

        @Override
        protected void onDraw(Canvas canvas) {
            if (mDuration <= 0) return;
            float y = getHeight() / 2f;
            float width = getWidth();
            float pos = mDragging ? mDragTargetMs : mPosition;
            float px = width * clamp((long) pos, 0, mDuration) / mDuration;
            Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
            paint.setStrokeCap(Paint.Cap.BUTT);
            // Measured from the reference frame (1672px wide, 408dpi device):
            // thin translucent base track, brighter chapter ticks on a ~79dp
            // period, red-to-magenta progress, solid pink thumb.
            paint.setStrokeWidth(dp(2.5f));
            paint.setColor(0x4DFFFFFF);
            canvas.drawLine(0, y, width, y, paint);
            paint.setStrokeWidth(dp(3.5f));
            paint.setColor(0x94FFFFFF);
            float tick = dp(19);
            float period = dp(79);
            for (float tx = period * 0.45f; tx + tick < width; tx += period) {
                canvas.drawLine(tx, y, tx + tick, y, paint);
            }
            if (px > dp(1)) {
                paint.setStrokeCap(Paint.Cap.ROUND);
                paint.setShader(
                        new android.graphics.LinearGradient(
                                0, y, px, y,
                                0xFFFF0754, 0xFFF333A2,
                                android.graphics.Shader.TileMode.CLAMP));
                canvas.drawLine(0, y, px, y, paint);
                paint.setShader(null);
            }
            paint.setColor(0xFFFF0F55);
            canvas.drawCircle(px, y, dp(8), paint);
        }

        @Override
        public boolean onTouchEvent(MotionEvent event) {
            if (mDuration <= 0) return false;
            float x = Math.max(0, Math.min(event.getX(), getWidth()));
            switch (event.getActionMasked()) {
                case MotionEvent.ACTION_DOWN:
                    mDragging = true;
                    mSeekBarTracking = true;
                    mHandler.removeCallbacks(mHideControls);
                    mDragTargetMs = (long) (x / getWidth() * mDuration);
                    showHud(formatTime(mDragTargetMs) + " / " + formatTime(mDuration));
                    invalidate();
                    return true;
                case MotionEvent.ACTION_MOVE:
                    if (mDragging) {
                        mDragTargetMs = (long) (x / getWidth() * mDuration);
                        showHud(formatTime(mDragTargetMs) + " / " + formatTime(mDuration));
                        invalidate();
                    }
                    return true;
                case MotionEvent.ACTION_UP:
                case MotionEvent.ACTION_CANCEL:
                    if (mDragging) {
                        mDragging = false;
                        mSeekBarTracking = false;
                        mPlayer.seekTo(mDragTargetMs);
                        hideHud();
                        scheduleHide();
                        invalidate();
                    }
                    return true;
            }
            return false;
        }
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

        LinearLayout speedRow = makeNavRow("Playback speed", speedText(mSpeed));
        mSpeedValue = (TextView) ((LinearLayout) speedRow).getChildAt(1);
        speedRow.setOnClickListener(
                v -> {
                    String[] opts = new String[SPEEDS.length];
                    int sel = 0;
                    for (int i = 0; i < SPEEDS.length; i++) {
                        opts[i] = speedText(SPEEDS[i]);
                        if (SPEEDS[i] == mSpeed) sel = i;
                    }
                    showSubSheet(
                            "Playback speed",
                            opts,
                            sel,
                            i -> {
                                setSpeed(SPEEDS[i]);
                            });
                });
        sheet.addView(speedRow);

        LinearLayout qualityRow = makeNavRow("Quality", "Auto");
        mQualityValue = (TextView) ((LinearLayout) qualityRow).getChildAt(1);
        qualityRow.setOnClickListener(
                v -> {
                    rebuildQualityOptions();
                    int sel = 0;
                    for (int i = 0; i < mQualityOptions.length; i++) {
                        if (mQualityOptions[i].equals(qualityText(mQualityHeight))) sel = i;
                    }
                    showSubSheet(
                            "Quality",
                            mQualityOptions,
                            sel,
                            i -> {
                                String label = mQualityOptions[i];
                                int h = label.equals("Auto")
                                        ? -1
                                        : Integer.parseInt(
                                                label.replaceAll("[^0-9]", ""));
                                setQuality(h);
                                mQualityHeight = h;
                                mQualityValue.setText(qualityText(h));
                            });
                });
        sheet.addView(qualityRow);

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

        LinearLayout autoRow =
                makeSheetRow(
                        "Open videos automatically",
                        FlashVideoPlayer.isAutoEnabled(this) ? "On" : "Off");
        autoRow.setOnClickListener(
                v -> {
                    boolean next = !FlashVideoPlayer.isAutoEnabled(this);
                    FlashVideoPlayer.setAutoEnabled(this, next);
                    ((TextView) ((LinearLayout) v).getChildAt(1)).setText(next ? "On" : "Off");
                });
        sheet.addView(autoRow);

        LinearLayout lockRow = makeSheetRow("Lock controls", "Off");
        lockRow.setOnClickListener(
                v -> {
                    toggleLock();
                    ((TextView) ((LinearLayout) v).getChildAt(1)).setText(mLocked ? "On" : "Off");
                    if (!mLocked) showSheet(true);
                });
        sheet.addView(lockRow);

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

    private String speedText(float speed) {
        if (speed == 1f) return "Normal";
        return (speed == (int) speed ? ((int) speed) + "x" : speed + "x");
    }

    private String qualityText(int height) {
        return height < 0 ? "Auto" : height + "p";
    }

    private void rebuildQualityOptions() {
        java.util.TreeSet<Integer> heights = new java.util.TreeSet<>(java.util.Collections.reverseOrder());
        try {
            for (androidx.media3.common.Tracks.Group group : mPlayer.getCurrentTracks().getGroups()) {
                if (group.getType() != androidx.media3.common.C.TRACK_TYPE_VIDEO) continue;
                for (int i = 0; i < group.length; i++) {
                    int h = group.getTrackFormat(i).height;
                    if (h > 0) heights.add(h);
                }
            }
        } catch (Exception e) {
            // Fall through with whatever was collected.
        }
        java.util.List<String> labels = new java.util.ArrayList<>();
        labels.add("Auto");
        if (heights.isEmpty()) {
            labels.add("Source");
        } else {
            for (int h : heights) labels.add(h + "p");
        }
        mQualityOptions = labels.toArray(new String[0]);
    }

    private void setQuality(int height) {
        try {
            androidx.media3.common.TrackSelectionParameters.Builder builder =
                    mPlayer.getTrackSelectionParameters().buildUpon();
            if (height < 0) {
                builder.setMaxVideoSize(Integer.MAX_VALUE, Integer.MAX_VALUE);
                builder.setMinVideoSize(Integer.MIN_VALUE, Integer.MIN_VALUE);
                builder.setForceLowestBitrate(false);
            } else {
                builder.setMaxVideoSize(Integer.MAX_VALUE, height);
                builder.setMinVideoSize(0, height);
            }
            mPlayer.setTrackSelectionParameters(builder.build());
        } catch (Exception e) {
            Toast.makeText(this, "Quality change not supported for this video",
                    Toast.LENGTH_SHORT).show();
        }
    }

    private LinearLayout makeNavRow(String title, String value) {
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER_VERTICAL);
        row.setPadding(0, dp(12), 0, dp(12));
        TextView titleView = new TextView(this);
        titleView.setText(title);
        titleView.setTextColor(Color.WHITE);
        titleView.setTextSize(14);
        row.addView(titleView, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        TextView valueView = new TextView(this);
        valueView.setText(value);
        valueView.setTextColor(Color.WHITE);
        valueView.setTextSize(14);
        row.addView(valueView);
        IconView chevron = new IconView(this, "next");
        chevron.setScaleX(0.55f);
        chevron.setScaleY(0.55f);
        chevron.setClickable(false);
        row.addView(chevron, new LinearLayout.LayoutParams(dp(18), dp(18)));
        return row;
    }

    private void showSubSheet(
            String title, String[] options, int selected, java.util.function.IntConsumer onPick) {
        if (mSubSheet == null) {
            mSubSheet = new LinearLayout(this);
            mSubSheet.setOrientation(LinearLayout.VERTICAL);
            GradientDrawable background = new GradientDrawable();
            background.setColor(0xFF212121);
            background.setCornerRadii(new float[] {
                    dp(12), dp(12), dp(12), dp(12), 0, 0, 0, 0});
            mSubSheet.setBackground(background);
            mSubSheet.setPadding(dp(16), dp(8), dp(16), dp(12));
            mSubSheet.setClickable(true);
            mSubSheet.setVisibility(View.GONE);
            mRoot.addView(
                    mSubSheet,
                    new FrameLayout.LayoutParams(
                            ViewGroup.LayoutParams.MATCH_PARENT,
                            ViewGroup.LayoutParams.WRAP_CONTENT,
                            Gravity.BOTTOM));
        }
        mSubSheet.removeAllViews();
        LinearLayout headerRow = new LinearLayout(this);
        headerRow.setOrientation(LinearLayout.HORIZONTAL);
        headerRow.setGravity(Gravity.CENTER_VERTICAL);
        IconView back = new IconView(this, "back");
        back.setClickable(false);
        headerRow.addView(back, new LinearLayout.LayoutParams(dp(22), dp(22)));
        TextView header = new TextView(this);
        header.setText(title);
        header.setTextColor(Color.WHITE);
        header.setTextSize(16);
        header.setTypeface(android.graphics.Typeface.DEFAULT_BOLD);
        header.setPadding(dp(10), dp(6), 0, dp(10));
        headerRow.addView(header, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        back.setOnClickListener(
                v -> {
                    mSubSheet.setVisibility(View.GONE);
                    showSheet(true);
                });
        mSubSheet.addView(headerRow);
        for (int i = 0; i < options.length; i++) {
            LinearLayout row = new LinearLayout(this);
            row.setOrientation(LinearLayout.HORIZONTAL);
            row.setGravity(Gravity.CENTER_VERTICAL);
            row.setPadding(0, dp(11), 0, dp(11));
            IconView check = new IconView(this, "check");
            check.setClickable(false);
            check.setVisibility(i == selected ? View.VISIBLE : View.INVISIBLE);
            row.addView(check, new LinearLayout.LayoutParams(dp(22), dp(22)));
            TextView label = new TextView(this);
            label.setText(options[i]);
            label.setTextColor(i == selected ? Color.WHITE : 0xDEFFFFFF);
            label.setTextSize(14);
            label.setPadding(dp(14), 0, 0, 0);
            row.addView(label, new LinearLayout.LayoutParams(0,
                    ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
            final int index = i;
            row.setOnClickListener(
                    v -> {
                        onPick.accept(index);
                        mSubSheet.setVisibility(View.GONE);
                        showSheet(true);
                    });
            mSubSheet.addView(row);
        }
        mHandler.removeCallbacks(mHideControls);
        mSheet.setVisibility(View.GONE);
        mSubSheet.setVisibility(View.VISIBLE);
        mScrim.setVisibility(View.VISIBLE);
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
        if (mSubSheet != null) mSubSheet.setVisibility(View.GONE);
        mSheet.setVisibility(show ? View.VISIBLE : View.GONE);
        mScrim.setVisibility(show ? View.VISIBLE : View.GONE);
        if (show) {
            mHandler.removeCallbacks(mHideControls);
        } else {
            scheduleHide();
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
                mHandler.removeCallbacks(mHideControls);
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
        if (mSpeedButton != null) {
            mSpeedButton.setText(speedText(speed));
        }
        if (mSpeedValue != null) {
            mSpeedValue.setText(speedText(speed));
        }
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
        mTimeView.setText(
                formatTime(mDragSeeking ? mDragSeekTarget : position)
                        + " / "
                        + (duration > 0 ? formatTime(duration) : "0:00"));
        mSeekBar.setProgress(position, duration, mPlayer.getBufferedPosition());
        if (mSubtitlesEnabled) {
            updateSubtitleText();
        }
    }

    private void updateSubtitleText() {
        try {
            if (!mPlayer.isCommandAvailable(Player.COMMAND_GET_TEXT)) {
                mSubtitleView.setVisibility(View.GONE);
                return;
            }
            StringBuilder text = new StringBuilder();
            for (androidx.media3.common.text.Cue cue : mPlayer.getCurrentCues().cues) {
                if (cue.text == null) continue;
                if (text.length() > 0) text.append('\n');
                text.append(cue.text);
            }
            if (text.length() == 0) {
                mSubtitleView.setVisibility(View.GONE);
            } else {
                mSubtitleView.setText(text);
                mSubtitleView.setVisibility(View.VISIBLE);
            }
        } catch (Exception e) {
            mSubtitleView.setVisibility(View.GONE);
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
        mCenterControls.setVisibility(visible && !mLocked ? View.VISIBLE : View.GONE);
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
        if (mLocked) {
            mLockIcon.setIcon("lock");
            mLockIcon.setVisibility(View.VISIBLE);
            mTopBar.setVisibility(View.GONE);
            mBottomBar.setVisibility(View.GONE);
            mCenterControls.setVisibility(View.GONE);
            mHandler.removeCallbacks(mHideControls);
            mHandler.postDelayed(
                    () -> {
                        if (mLocked) mLockIcon.setVisibility(View.GONE);
                    },
                    HIDE_DELAY_MS);
        } else {
            mLockIcon.setVisibility(View.GONE);
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
        int w;
        int h;
        if (mAspectMode == 2) {
            w = width;
            h = height;
        } else if (mAspectMode == 1) {
            // Cover: fill the screen, cropping the overflowing axis.
            w = width;
            h = Math.round(w / videoRatio);
            if (h < height) {
                h = height;
                w = Math.round(h * videoRatio);
            }
        } else {
            w = width;
            h = Math.round(w / videoRatio);
            if (h > height) {
                h = height;
                w = Math.round(h * videoRatio);
            }
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
        if (mPlayer != null && !mBackgroundPlay) {
            mWasPlayingOnPause = mPlayer.isPlaying();
            mPlayer.pause();
        }
    }

    @Override
    protected void onStop() {
        super.onStop();
        if (mPlayer != null && !mBackgroundPlay) mPlayer.pause();
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
            mPaint.setStrokeWidth(dp(1.6f));
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
            // The glyph keeps a fixed share of the touch box so shrinking the
            // bars does not also shrink the icon inside them.
            float s = Math.min(getWidth(), getHeight()) / 24f * 0.62f;
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
                case "check":
                    path.moveTo(5, 13);
                    path.lineTo(10, 18);
                    path.lineTo(19, 7);
                    canvas.drawPath(path, mPaint);
                    break;
                case "chevron":
                    path.moveTo(6, 9);
                    path.lineTo(12, 15);
                    path.lineTo(18, 9);
                    canvas.drawPath(path, mPaint);
                    break;
                case "prev":
                    path.moveTo(16, 5);
                    path.lineTo(8, 12);
                    path.lineTo(16, 19);
                    path.close();
                    canvas.drawPath(path, fill);
                    canvas.drawRect(5, 5, 7, 19, fill);
                    break;
                case "next":
                    path.moveTo(8, 5);
                    path.lineTo(16, 12);
                    path.lineTo(8, 19);
                    path.close();
                    canvas.drawPath(path, fill);
                    canvas.drawRect(17, 5, 19, 19, fill);
                    break;
                case "expand":
                    canvas.drawLine(5, 9, 5, 5, mPaint);
                    canvas.drawLine(5, 5, 9, 5, mPaint);
                    canvas.drawLine(15, 5, 19, 5, mPaint);
                    canvas.drawLine(19, 5, 19, 9, mPaint);
                    canvas.drawLine(19, 15, 19, 19, mPaint);
                    canvas.drawLine(19, 19, 15, 19, mPaint);
                    canvas.drawLine(9, 19, 5, 19, mPaint);
                    canvas.drawLine(5, 19, 5, 15, mPaint);
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
                case "cast":
                    canvas.drawRoundRect(3, 4, 21, 14, 2, 2, mPaint);
                    canvas.drawCircle(6, 21, 1.3f, fill);
                    canvas.drawArc(new android.graphics.RectF(3, 18, 9, 24), 270, 90, false, mPaint);
                    canvas.drawArc(new android.graphics.RectF(0, 15, 12, 27), 270, 90, false, mPaint);
                    break;
                case "cc":
                    canvas.drawRoundRect(3, 6, 21, 18, 3, 3, mPaint);
                    Paint ccText = new Paint(Paint.ANTI_ALIAS_FLAG);
                    ccText.setColor(mPaint.getColor());
                    ccText.setStyle(Paint.Style.FILL);
                    ccText.setTextAlign(Paint.Align.CENTER);
                    ccText.setTextSize(8f);
                    ccText.setFakeBoldText(true);
                    canvas.drawText("CC", 12, 14.9f, ccText);
                    break;
                case "headphones":
                    path.moveTo(4, 15);
                    path.lineTo(4, 13);
                    path.arcTo(new android.graphics.RectF(4, 4, 20, 20), 180, 180);
                    path.lineTo(20, 15);
                    canvas.drawPath(path, mPaint);
                    canvas.drawRoundRect(3, 13, 7, 20, 2, 2, fill);
                    canvas.drawRoundRect(17, 13, 21, 20, 2, 2, fill);
                    break;
                case "monogram":
                    canvas.drawCircle(12, 12, 8.5f, mPaint);
                    Paint monoText = new Paint(Paint.ANTI_ALIAS_FLAG);
                    monoText.setColor(mPaint.getColor());
                    monoText.setStyle(Paint.Style.FILL);
                    monoText.setTextAlign(Paint.Align.CENTER);
                    monoText.setTextSize(10f);
                    monoText.setFakeBoldText(true);
                    canvas.drawText("T", 12, 15.6f, monoText);
                    break;
                case "dots":
                    canvas.drawCircle(12, 5.5f, 1.6f, fill);
                    canvas.drawCircle(12, 12, 1.6f, fill);
                    canvas.drawCircle(12, 18.5f, 1.6f, fill);
                    break;
            }
            canvas.restore();
        }
    }
}