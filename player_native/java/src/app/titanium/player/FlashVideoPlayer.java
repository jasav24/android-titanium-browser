package app.titanium.player;

import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.os.Handler;
import android.os.Looper;
import android.widget.Toast;

import org.chromium.base.Log;
import org.chromium.content_public.browser.WebContents;
import org.chromium.chrome.browser.ChromeActivity;
import org.chromium.chrome.browser.tab.Tab;
import org.chromium.chrome.browser.tab.TabObserver;

import org.json.JSONArray;
import org.json.JSONObject;

import java.lang.ref.WeakReference;

/**
 * Entry points for the native video player: the video context-menu row and the
 * app-menu "Video player" row. Scans the page for a playable stream, pauses it,
 * and hands the URL to {@link FlashPlayerActivity}.
 */
public final class FlashVideoPlayer {
    private static final String TAG = "FlashVideoPlayer";

    // Runs in the page's main world through WebContents.evaluateJavaScript (the
    // CanExecuteJavaScript gate is opened by patch.sh). Same-origin iframes are
    // scanned too; cross-origin ones are unreachable from the main world.
    private static final String SCAN_JS =
            "(function(){var out=[];function scan(d){var vs=d.getElementsByTagName('video');"
            + "for(var i=0;i<vs.length;i++){var v=vs[i];var u=v.currentSrc||v.src||'';"
            + "if(!u){var ss=v.getElementsByTagName('source');"
            + "for(var j=0;j<ss.length;j++){if(ss[j].src){u=ss[j].src;break;}}}"
            + "if(!/^https?:/i.test(u))continue;var r=v.getBoundingClientRect();"
            + "out.push({u:u,t:d.title||'',p:v.currentTime||0,a:r.width*r.height,"
            + "pl:(!v.paused&&!v.ended)?1:0});}"
            + "var fs=d.getElementsByTagName('iframe');"
            + "for(var k=0;k<fs.length;k++){try{if(fs[k].contentDocument)"
            + "scan(fs[k].contentDocument);}catch(e){}}}"
            + "scan(document);"
            + "out.sort(function(x,y){return (y.pl-x.pl)||(y.a-x.a);});"
            + "return JSON.stringify(out.slice(0,8));})()";

    public static void openFromTab(Context context, Tab tab) {
        WebContents wc = tab == null ? null : tab.getWebContents();
        openFromWebContents(context, wc, null);
    }

    // ------------------------------------------------------- auto-open

    private static boolean sAttached;
    private static String sLastAutoUrl;
    private static WeakReference<Activity> sActivityRef;
    private static WeakReference<Tab> sPollTab;
    private static final Handler sMainHandler = new Handler(Looper.getMainLooper());
    private static final long AUTO_MIN_AREA = 60_000;

    public static boolean isAutoEnabled(Context context) {
        return context.getSharedPreferences("titanium_player", Context.MODE_PRIVATE)
                .getBoolean("auto", true);
    }

    public static void setAutoEnabled(Context context, boolean enabled) {
        context.getSharedPreferences("titanium_player", Context.MODE_PRIVATE)
                .edit()
                .putBoolean("auto", enabled)
                .apply();
    }

    /**
     * Called once from ChromeTabbedActivity.onFinishNativeInitialization:
     * watches the visible tab and, while a page video starts playing on its
     * own, launches the native player for it without a long press. Polling
     * reuses SCAN_JS, so the auto path and the menu path pick the same video.
     */
    public static void attach(Context context) {
        if (sAttached || !(context instanceof ChromeActivity)) return;
        sAttached = true;
        sActivityRef = new WeakReference<>((Activity) context);
        ChromeActivity activity = (ChromeActivity) context;
        activity.getTabModelSelector()
                .getModel(false)
                .addObserver(
                        new TabObserver() {
                            @Override
                            public void onShown(Tab tab) {
                                startPolling(tab);
                            }

                            @Override
                            public void onHidden(Tab tab) {
                                if (sPollTab != null && sPollTab.get() == tab) {
                                    sPollTab = null;
                                    sMainHandler.removeCallbacks(sPollRunnable);
                                }
                            }
                        });
        startPolling(activity.getActivityTab());
    }

    private static void startPolling(Tab tab) {
        sPollTab = new WeakReference<>(tab);
        sMainHandler.removeCallbacks(sPollRunnable);
        sMainHandler.postDelayed(sPollRunnable, 1200);
    }

    private static final Runnable sPollRunnable =
            new Runnable() {
                @Override
                public void run() {
                    Tab tab = sPollTab == null ? null : sPollTab.get();
                    Activity activity = sActivityRef == null ? null : sActivityRef.get();
                    if (tab == null || tab.isDestroyed() || activity == null) return;
                    WebContents wc = tab.getWebContents();
                    if (wc == null || wc.isDestroyed()) return;
                    if (isAutoEnabled(activity)) {
                        wc.evaluateJavaScript(SCAN_JS, FlashVideoPlayer::onAutoScan);
                    }
                    sMainHandler.postDelayed(this, 2500);
                }
            };

    private static void onAutoScan(String result) {
        Tab tab = sPollTab == null ? null : sPollTab.get();
        Activity activity = sActivityRef == null ? null : sActivityRef.get();
        if (tab == null || tab.isDestroyed() || activity == null) return;
        try {
            JSONArray candidates = new JSONArray(new JSONArray("[" + result + "]").getString(0));
            for (int i = 0; i < candidates.length(); i++) {
                JSONObject c = candidates.getJSONObject(i);
                if (c.optInt("pl") != 1 || c.optInt("a") < AUTO_MIN_AREA) continue;
                String url = c.getString("u");
                if (url.equals(sLastAutoUrl)) return;
                sLastAutoUrl = url;
                WebContents wc = tab.getWebContents();
                if (wc == null || wc.isDestroyed()) return;
                pausePageVideo(wc, url);
                launch(
                        activity,
                        url,
                        c.optString("t"),
                        (long) (c.optDouble("p", 0) * 1000),
                        wc.getVisibleUrl().getSpec());
                return;
            }
        } catch (Exception e) {
            Log.w(TAG, "auto scan failed: %s", e.toString());
        }
    }

    public static void openFromWebContents(
            Context context, WebContents webContents, String preferredUrl) {
        if (webContents == null || webContents.isDestroyed()) {
            if (preferredUrl != null) {
                launch(context, preferredUrl, "", 0L, null);
            } else {
                toast(context, "No playable video found on this page");
            }
            return;
        }
        final String pageUrl = webContents.getVisibleUrl().getSpec();
        webContents.evaluateJavaScript(
                SCAN_JS,
                result -> onScanResult(context, webContents, preferredUrl, pageUrl, result));
    }

    private static void onScanResult(
            Context context,
            WebContents webContents,
            String preferredUrl,
            String pageUrl,
            String result) {
        try {
            // evaluateJavaScript delivers the script result JSON-encoded, so a
            // string result arrives quoted and needs one decode first.
            JSONArray candidates = new JSONArray(new JSONArray("[" + result + "]").getString(0));
            JSONObject best = null;
            if (preferredUrl != null) {
                for (int i = 0; i < candidates.length(); i++) {
                    JSONObject c = candidates.getJSONObject(i);
                    if (preferredUrl.equals(c.optString("u"))) {
                        best = c;
                        break;
                    }
                }
            }
            if (best == null && candidates.length() > 0) {
                best = candidates.getJSONObject(0);
            }
            if (best != null) {
                String url = best.getString("u");
                sLastAutoUrl = url;
                pausePageVideo(webContents, url);
                launch(
                        context,
                        url,
                        best.optString("t"),
                        (long) (best.optDouble("p", 0) * 1000),
                        pageUrl);
                return;
            }
        } catch (Exception e) {
            Log.w(TAG, "page scan failed: %s", e.toString());
        }
        if (preferredUrl != null) {
            sLastAutoUrl = preferredUrl;
            pausePageVideo(webContents, preferredUrl);
            launch(context, preferredUrl, "", 0L, pageUrl);
        } else {
            toast(context, "No playable video found on this page");
        }
    }

    private static void pausePageVideo(WebContents webContents, String url) {
        if (webContents.isDestroyed()) return;
        String quoted = JSONObject.quote(url);
        webContents.evaluateJavaScript(
                "(function(){var vs=document.getElementsByTagName('video');"
                        + "for(var i=0;i<vs.length;i++){var v=vs[i];"
                        + "if((v.currentSrc||v.src)==="
                        + quoted
                        + "){v.pause();}}})()",
                null);
    }

    private static void launch(
            Context context, String url, String title, long positionMs, String pageUrl) {
        Intent intent = new Intent(context, FlashPlayerActivity.class);
        intent.putExtra(FlashPlayerActivity.EXTRA_URL, url);
        intent.putExtra(FlashPlayerActivity.EXTRA_TITLE, title == null ? "" : title);
        intent.putExtra(FlashPlayerActivity.EXTRA_POSITION_MS, positionMs);
        intent.putExtra(FlashPlayerActivity.EXTRA_PAGE_URL, pageUrl == null ? "" : pageUrl);
        context.startActivity(intent);
    }

    private static void toast(Context context, String message) {
        Toast.makeText(context, message, Toast.LENGTH_SHORT).show();
    }

    private FlashVideoPlayer() {}
}