/*
 * Headless EGL/GLES3 probe for voronoi_gap_norm / voronoi_edge_norm.
 *
 * Build:
 *   cc -O2 -o /tmp/voronoi-harness home/voronoi-glsl-harness.c -lEGL -lGLESv2
 *
 * Usage:
 *   /tmp/voronoi-harness probe.frag <variant> <q_norm_x> <q_norm_y>
 *   prints: "<gap> <edge>\n"
 *
 * Called by home/voronoi-shader-test.py --gpu
 */
#define _GNU_SOURCE
#include <EGL/egl.h>
#include <GLES3/gl3.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static char *read_file(const char *path) {
    FILE *f = fopen(path, "rb");
    if (!f) {
        perror(path);
        return NULL;
    }
    fseek(f, 0, SEEK_END);
    long sz = ftell(f);
    fseek(f, 0, SEEK_SET);
    char *buf = malloc((size_t)sz + 1);
    if (!buf) {
        fclose(f);
        return NULL;
    }
    if (fread(buf, 1, (size_t)sz, f) != (size_t)sz) {
        fclose(f);
        free(buf);
        return NULL;
    }
    fclose(f);
    buf[sz] = '\0';
    return buf;
}

static GLuint compile_shader(GLenum type, const char *src) {
    GLuint shader = glCreateShader(type);
    glShaderSource(shader, 1, &src, NULL);
    glCompileShader(shader);
    GLint ok = 0;
    glGetShaderiv(shader, GL_COMPILE_STATUS, &ok);
    if (!ok) {
        char log[8192];
        GLsizei len = 0;
        glGetShaderInfoLog(shader, sizeof log, &len, log);
        fprintf(stderr, "shader compile failed:\n%.*s\n", (int)len, log);
        glDeleteShader(shader);
        return 0;
    }
    return shader;
}

static const char *VERT_SRC =
    "#version 300 es\n"
    "in vec2 a_pos;\n"
    "void main() { gl_Position = vec4(a_pos, 0.0, 1.0); }\n";

int main(int argc, char **argv) {
    if (argc >= 3 && strcmp(argv[1], "--compile-only") == 0) {
        const char *frag_path = argv[2];
        char *frag_src = read_file(frag_path);
        if (!frag_src)
            return 1;
        EGLDisplay dpy = eglGetDisplay(EGL_DEFAULT_DISPLAY);
        if (dpy == EGL_NO_DISPLAY || !eglInitialize(dpy, NULL, NULL))
            return 1;
        const EGLint cfg_attribs[] = {
            EGL_SURFACE_TYPE, EGL_PBUFFER_BIT,
            EGL_RENDERABLE_TYPE, EGL_OPENGL_ES3_BIT,
            EGL_NONE,
        };
        EGLConfig cfg;
        EGLint n_cfg = 0;
        eglChooseConfig(dpy, cfg_attribs, &cfg, 1, &n_cfg);
        const EGLint pbuf_attribs[] = {EGL_WIDTH, 1, EGL_HEIGHT, 1, EGL_NONE};
        EGLSurface surf = eglCreatePbufferSurface(dpy, cfg, pbuf_attribs);
        const EGLint ctx_attribs[] = {EGL_CONTEXT_CLIENT_VERSION, 3, EGL_NONE};
        EGLContext ctx = eglCreateContext(dpy, cfg, EGL_NO_CONTEXT, ctx_attribs);
        if (!eglMakeCurrent(dpy, surf, surf, ctx))
            return 1;
        GLuint vert = compile_shader(GL_VERTEX_SHADER, VERT_SRC);
        GLuint frag = compile_shader(GL_FRAGMENT_SHADER, frag_src);
        free(frag_src);
        if (!vert || !frag)
            return 1;
        GLuint prog = glCreateProgram();
        glAttachShader(prog, vert);
        glAttachShader(prog, frag);
        glBindAttribLocation(prog, 0, "a_pos");
        glLinkProgram(prog);
        GLint ok = 0;
        glGetProgramiv(prog, GL_LINK_STATUS, &ok);
        if (!ok) {
            char log[8192];
            GLsizei len = 0;
            glGetProgramInfoLog(prog, sizeof log, &len, log);
            fprintf(stderr, "link failed:\n%.*s\n", (int)len, log);
            return 1;
        }
        printf("compile_ok bytes=%s\n", frag_path);
        return 0;
    }

    if (argc < 5) {
        fprintf(stderr, "usage: %s <probe.frag> <variant> <q_x> <q_y>\n", argv[0]);
        fprintf(stderr, "   or: %s --compile-only <probe.frag>\n", argv[0]);
        return 1;
    }
    const char *frag_path = argv[1];
    int variant = atoi(argv[2]);
    float qx = (float)atof(argv[3]);
    float qy = (float)atof(argv[4]);

    char *frag_src = read_file(frag_path);
    if (!frag_src)
        return 1;

    EGLDisplay dpy = eglGetDisplay(EGL_DEFAULT_DISPLAY);
    if (dpy == EGL_NO_DISPLAY) {
        fprintf(stderr, "eglGetDisplay failed\n");
        return 1;
    }
    if (!eglInitialize(dpy, NULL, NULL)) {
        fprintf(stderr, "eglInitialize failed\n");
        return 1;
    }

    const EGLint cfg_attribs[] = {
        EGL_SURFACE_TYPE, EGL_PBUFFER_BIT,
        EGL_RENDERABLE_TYPE, EGL_OPENGL_ES3_BIT,
        EGL_NONE,
    };
    EGLConfig cfg;
    EGLint n_cfg = 0;
    if (!eglChooseConfig(dpy, cfg_attribs, &cfg, 1, &n_cfg) || n_cfg == 0) {
        fprintf(stderr, "eglChooseConfig failed\n");
        return 1;
    }

    const EGLint pbuf_attribs[] = {EGL_WIDTH, 1, EGL_HEIGHT, 1, EGL_NONE};
    EGLSurface surf = eglCreatePbufferSurface(dpy, cfg, pbuf_attribs);
    if (surf == EGL_NO_SURFACE) {
        fprintf(stderr, "eglCreatePbufferSurface failed\n");
        return 1;
    }

    const EGLint ctx_attribs[] = {EGL_CONTEXT_CLIENT_VERSION, 3, EGL_NONE};
    EGLContext ctx = eglCreateContext(dpy, cfg, EGL_NO_CONTEXT, ctx_attribs);
    if (ctx == EGL_NO_CONTEXT) {
        fprintf(stderr, "eglCreateContext failed\n");
        return 1;
    }
    if (!eglMakeCurrent(dpy, surf, surf, ctx)) {
        fprintf(stderr, "eglMakeCurrent failed\n");
        return 1;
    }

    GLuint vert = compile_shader(GL_VERTEX_SHADER, VERT_SRC);
    GLuint frag = compile_shader(GL_FRAGMENT_SHADER, frag_src);
    free(frag_src);
    if (!vert || !frag)
        return 1;

    GLuint prog = glCreateProgram();
    glAttachShader(prog, vert);
    glAttachShader(prog, frag);
    glBindAttribLocation(prog, 0, "a_pos");
    glLinkProgram(prog);
    glDeleteShader(vert);
    glDeleteShader(frag);

    GLint ok = 0;
    glGetProgramiv(prog, GL_LINK_STATUS, &ok);
    if (!ok) {
        char log[8192];
        GLsizei len = 0;
        glGetProgramInfoLog(prog, sizeof log, &len, log);
        fprintf(stderr, "link failed:\n%.*s\n", (int)len, log);
        return 1;
    }

    glUseProgram(prog);
    glUniform1i(glGetUniformLocation(prog, "u_variant"), variant);
    glUniform2f(glGetUniformLocation(prog, "u_q_norm"), qx, qy);

    GLuint fbo = 0, rb = 0;
    glGenRenderbuffers(1, &rb);
    glBindRenderbuffer(GL_RENDERBUFFER, rb);
    glRenderbufferStorage(GL_RENDERBUFFER, GL_RGBA32F, 1, 1);
    glGenFramebuffers(1, &fbo);
    glBindFramebuffer(GL_FRAMEBUFFER, fbo);
    glFramebufferRenderbuffer(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_RENDERBUFFER, rb);

    const GLfloat quad[] = {-1.f, -1.f, 1.f, -1.f, -1.f, 1.f, 1.f, 1.f};
    glViewport(0, 0, 1, 1);
    glClear(GL_COLOR_BUFFER_BIT);
    glEnableVertexAttribArray(0);
    glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 0, quad);
    glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);

    float px[4];
    glReadPixels(0, 0, 1, 1, GL_RGBA, GL_FLOAT, px);
    printf("%.9g %.9g\n", px[0], px[1]);

    eglMakeCurrent(dpy, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT);
    eglDestroyContext(dpy, ctx);
    eglDestroySurface(dpy, surf);
    eglTerminate(dpy);
    return 0;
}
