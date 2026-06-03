/*
 * Copyright (C) 2026 Ben Oberkfell.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package com.oberkfell.viewspector.agent;

import android.util.Log;

import dalvik.system.DexClassLoader;

import java.io.File;
import java.lang.reflect.Method;

/**
 * ViewSpector bootstrap bridge (layer 2 of 3).
 *
 * <p>This class is added to the <em>bootstrap</em> classloader by the native JVMTI agent via
 * {@code AddToBootstrapClassLoaderSearch(bootstrap.dex)}, then located with {@code FindClass} and
 * invoked through {@link #initialize(String, String)}. See the native agent's {@code Agent_OnAttach}
 * (modeled on {@code tools-base/ui-inspector/agent/native/agent.cc:53-131}) and the equivalent Java
 * bridge {@code tools-base/ui-inspector/agent/service/.../InspectorService.java:45-90}.
 *
 * <p>Its sole job is to construct a {@link DexClassLoader} for {@code payload.jar} whose
 * <strong>parent is the application classloader</strong>, then hand control to the Kotlin payload's
 * {@code Payload.start(socketName)} entry point. The app-classloader parentage is mandatory: the
 * payload must be able to resolve AndroidX / Material {@code *$InspectionCompanion} classes that only
 * exist on the application classpath (see CONTRACT.md section 2, and {@code InspectorContext.java:115-153}).
 *
 * <p>Because this class lives in the bootstrap classloader, it must reference ONLY framework classes
 * ({@code android.*}, {@code dalvik.system.*}) and {@code java.*} reflection. It must never touch any
 * application or Kotlin-stdlib class directly — those are resolved lazily, by name, through the child
 * {@link DexClassLoader}.
 */
public final class Bootstrap {

    private static final String TAG = "ViewSpector";

    /** Fully-qualified name of the Kotlin payload entry point. Loaded reflectively via the child CL. */
    private static final String PAYLOAD_CLASS_NAME =
            "com.oberkfell.viewspector.agent.payload.Payload";

    /** Static entry point on {@link #PAYLOAD_CLASS_NAME}: {@code public static void start(String socketName)}. */
    private static final String PAYLOAD_START_METHOD = "start";

    /** Name of the thread on which the payload is launched, so it never blocks the JVMTI attach thread. */
    private static final String LAUNCH_THREAD_NAME = "ViewSpector-Bootstrap";

    private Bootstrap() {
        // Static-only entry-point holder; not instantiable.
    }

    /**
     * Native-agent entry point. Loads the payload jar with the application classloader as its parent
     * and invokes {@code Payload.start(socketName)} on a dedicated worker thread.
     *
     * <p>This method is invoked on the JVMTI attach thread and MUST return promptly, so the actual
     * payload launch is dispatched to {@link #LAUNCH_THREAD_NAME}. All failures are caught and logged
     * (CONTRACT.md section 6/8: never crash the host application; log under tag {@code "ViewSpector"}).
     *
     * @param payloadPath absolute filesystem path to {@code payload.jar} (already copied into the
     *                    application data dir by the host's {@code run-as cp}; see CONTRACT.md section 2).
     * @param socketName  abstract {@code LocalServerSocket} name the payload should bind, e.g.
     *                    {@code viewspector_<pid>} (CONTRACT.md section 3).
     */
    public static void initialize(final String payloadPath, final String socketName) {
        try {
            if (payloadPath == null || payloadPath.isEmpty()) {
                Log.e(TAG, "initialize: payloadPath is null/empty; aborting bootstrap");
                return;
            }
            if (socketName == null || socketName.isEmpty()) {
                Log.e(TAG, "initialize: socketName is null/empty; aborting bootstrap");
                return;
            }

            final File payloadFile = new File(payloadPath);
            if (!payloadFile.exists()) {
                Log.e(TAG, "initialize: payload jar does not exist at " + payloadPath
                        + "; aborting bootstrap");
                return;
            }

            final ClassLoader appClassLoader = findAppClassLoader();
            if (appClassLoader == null) {
                Log.e(TAG, "initialize: could not locate an application ClassLoader; "
                        + "aborting bootstrap");
                return;
            }

            final String optimizedDir = resolveOptimizedDir(payloadFile);

            // Child of the app classloader so the payload can resolve AndroidX / Material
            // *$InspectionCompanion classes (CONTRACT.md section 2). Mirrors
            // InspectorContext.createClassloader (InspectorContext.java:144-153) and
            // InspectorService.initialize (InspectorService.java:58-63), but with an explicit,
            // writable optimized-dex output directory.
            final DexClassLoader payloadClassLoader =
                    new DexClassLoader(payloadPath, optimizedDir, null, appClassLoader);

            final Class<?> payloadClass =
                    Class.forName(PAYLOAD_CLASS_NAME, true, payloadClassLoader);
            final Method startMethod =
                    payloadClass.getMethod(PAYLOAD_START_METHOD, String.class);

            // Launch on a dedicated thread. Payload.start spawns its own accept-loop thread and is
            // expected to return quickly, but we must never block the JVMTI attach thread on it.
            final Thread launchThread = new Thread(new Runnable() {
                @Override
                public void run() {
                    try {
                        startMethod.invoke(null, socketName);
                        Log.i(TAG, "initialize: payload started on socket '" + socketName + "'");
                    } catch (Throwable t) {
                        Log.e(TAG, "initialize: error invoking "
                                + PAYLOAD_CLASS_NAME + "." + PAYLOAD_START_METHOD, t);
                    }
                }
            }, LAUNCH_THREAD_NAME);
            launchThread.setDaemon(true);
            launchThread.start();
        } catch (Throwable t) {
            // Catch Throwable (incl. LinkageError / ReflectiveOperationException) so a bootstrap
            // failure can never bring down the inspected application.
            Log.e(TAG, "initialize: failed to bootstrap ViewSpector payload", t);
        }
    }

    /**
     * Locates the application's {@link ClassLoader}, which becomes the parent of the payload's
     * {@link DexClassLoader}.
     *
     * <p>Strategy order (CONTRACT.md / module spec):
     * <ol>
     *   <li>Primary: reflect {@code android.app.ActivityThread.currentApplication().getClassLoader()}
     *       — this yields the real application classpath (mirrors
     *       {@code AppInspectionService.findClassLoader}, AppInspectionService.java:451-472, but using
     *       reflection rather than ART tooling which is unavailable to the bootstrap layer).</li>
     *   <li>Fallback: {@code Thread.currentThread().getContextClassLoader()}.</li>
     *   <li>Fallback: the main {@code Looper} thread's context classloader (mirrors
     *       {@code InspectorService.getAppClassLoader}, InspectorService.java:82-90).</li>
     * </ol>
     *
     * @return the best available application classloader, or {@code null} if none could be found.
     */
    private static ClassLoader findAppClassLoader() {
        // (1) Primary: ActivityThread.currentApplication().getClassLoader()
        try {
            final Class<?> activityThreadClass =
                    Class.forName("android.app.ActivityThread");
            final Method currentApplication =
                    activityThreadClass.getMethod("currentApplication");
            final Object application = currentApplication.invoke(null);
            if (application != null) {
                // android.app.Application extends ContextWrapper -> Context.getClassLoader().
                final Method getClassLoader =
                        application.getClass().getMethod("getClassLoader");
                final Object loader = getClassLoader.invoke(application);
                if (loader instanceof ClassLoader) {
                    return (ClassLoader) loader;
                }
            }
        } catch (Throwable t) {
            Log.w(TAG, "findAppClassLoader: ActivityThread.currentApplication() strategy failed; "
                    + "trying fallbacks", t);
        }

        // (2) Fallback: the attach thread's context classloader.
        try {
            final ClassLoader contextLoader =
                    Thread.currentThread().getContextClassLoader();
            if (contextLoader != null) {
                return contextLoader;
            }
        } catch (Throwable t) {
            Log.w(TAG, "findAppClassLoader: context-classloader fallback failed", t);
        }

        // (3) Fallback: main looper thread's context classloader.
        try {
            final android.os.Looper mainLooper = android.os.Looper.getMainLooper();
            if (mainLooper != null) {
                final Thread mainThread = mainLooper.getThread();
                if (mainThread != null) {
                    final ClassLoader looperLoader = mainThread.getContextClassLoader();
                    if (looperLoader != null) {
                        return looperLoader;
                    }
                }
            }
        } catch (Throwable t) {
            Log.w(TAG, "findAppClassLoader: main-looper-classloader fallback failed", t);
        }

        return null;
    }

    /**
     * Resolves a writable directory for the {@link DexClassLoader}'s optimized-dex output.
     *
     * <p>Prefers the directory containing the payload jar (the application data dir after the host's
     * {@code run-as cp}, which is process-writable). Falls back to {@code java.io.tmpdir}
     * (cf. {@code InspectorContext.createClassloader}, InspectorContext.java:145).
     *
     * @param payloadFile the payload jar file.
     * @return an absolute path to a writable directory, or {@code null} to let the platform choose
     *         a default (a {@code null} optimizedDir is accepted by {@link DexClassLoader}).
     */
    private static String resolveOptimizedDir(final File payloadFile) {
        // Prefer the payload jar's own parent dir (the app data dir).
        try {
            final File parent = payloadFile.getParentFile();
            if (parent != null) {
                final File oat = new File(parent, "viewspector_oat");
                if (oat.isDirectory() || oat.mkdirs()) {
                    if (oat.canWrite()) {
                        return oat.getAbsolutePath();
                    }
                }
                if (parent.isDirectory() && parent.canWrite()) {
                    return parent.getAbsolutePath();
                }
            }
        } catch (Throwable t) {
            Log.w(TAG, "resolveOptimizedDir: payload-parent strategy failed; trying tmpdir", t);
        }

        // Fallback: java.io.tmpdir.
        try {
            final String tmp = System.getProperty("java.io.tmpdir");
            if (tmp != null && !tmp.isEmpty()) {
                final File tmpDir = new File(tmp);
                if (tmpDir.isDirectory() && tmpDir.canWrite()) {
                    return tmpDir.getAbsolutePath();
                }
            }
        } catch (Throwable t) {
            Log.w(TAG, "resolveOptimizedDir: tmpdir fallback failed", t);
        }

        // Let DexClassLoader pick a platform default.
        return null;
    }
}
