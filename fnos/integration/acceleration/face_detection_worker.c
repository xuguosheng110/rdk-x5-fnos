/* SPDX-License-Identifier: Apache-2.0
 * X5 native Photos detector: official mono2d face boxes + 106-point landmarks.
 * Model post-processing follows D-Robotics/hobot_dnn and
 * D-Robotics/face_landmarks_detection (Copyright 2024 D-Robotics).
 * No ROS runtime. Wire: X5FD; repeated 960x544 NV12 -> u32 count followed by
 * count*(5 box floats + 106*(x,y,confidence) floats), all little endian.
 */
#define _POSIX_C_SOURCE 200809L
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include "dnn/hb_dnn.h"
#define WIDTH 960
#define HEIGHT 544
#define PIXELS (WIDTH*HEIGHT)
#define MAX_FACES 30
#define LANDMARKS 106
#define REQUIRE(x) do { if (!(x)) { fprintf(stderr,"contract failed: %s\n",#x); return -1; } } while (0)
#define SDK(x) do { int e=(x); if(e) { fprintf(stderr,"%s: %d\n",#x,e); return -1; } } while (0)
typedef struct { hbPackedDNNHandle_t packed; hbDNNHandle_t handle; hbDNNTensor in, out[9]; int outputs; } Model;
static int initialize(Model *m,const char *file,int outputs,int width,int height) {
    const char **names=NULL;int n=0;
    SDK(hbDNNInitializeFromFiles(&m->packed,&file,1));
    SDK(hbDNNGetModelNameList(&names,&n,m->packed));REQUIRE(n==1);
    SDK(hbDNNGetModelHandle(&m->handle,m->packed,names[0]));
    SDK(hbDNNGetInputCount(&n,m->handle));REQUIRE(n==1);
    SDK(hbDNNGetOutputCount(&m->outputs,m->handle));REQUIRE(m->outputs==outputs);
    SDK(hbDNNGetInputTensorProperties(&m->in.properties,m->handle,0));
    const hbDNNTensorProperties *p=&m->in.properties;
    REQUIRE(p->tensorType==HB_DNN_IMG_TYPE_NV12_SEPARATE && p->tensorLayout==HB_DNN_LAYOUT_NCHW);
    REQUIRE(p->validShape.numDimensions==4 && p->validShape.dimensionSize[2]==height && p->validShape.dimensionSize[3]==width);
    SDK(hbSysAllocCachedMem(&m->in.sysMem[0],width*height));
    SDK(hbSysAllocCachedMem(&m->in.sysMem[1],width*height/2));
    for(int i=0;i<outputs;i++) {
        SDK(hbDNNGetOutputTensorProperties(&m->out[i].properties,m->handle,i));
        REQUIRE(m->out[i].properties.alignedByteSize>0 && m->out[i].properties.alignedByteSize<8*1024*1024);
        SDK(hbSysAllocCachedMem(&m->out[i].sysMem[0],m->out[i].properties.alignedByteSize));
    }
    return 0;
}
static int infer(Model *m,hbDNNTensor *input,hbDNNRoi *roi) {
    hbDNNTaskHandle_t task=NULL;hbDNNTensor *out=m->out;hbDNNInferCtrlParam ctrl;
    HB_DNN_INITIALIZE_INFER_CTRL_PARAM(&ctrl);
    if(roi) { SDK(hbDNNRoiInfer(&task,&out,input,roi,1,m->handle,&ctrl)); }
    else { SDK(hbDNNInfer(&task,&out,input,m->handle,&ctrl)); }
    int rc=hbDNNWaitTaskDone(task,10000);
    if(rc) { fprintf(stderr,"X5 face inference failed: %d\n",rc);_Exit(3); }
    SDK(hbDNNReleaseTask(task));
    for(int i=0;i<m->outputs;i++) SDK(hbSysFlushMem(&m->out[i].sysMem[0],HB_SYS_MEM_CACHE_INVALIDATE));
    return 0;
}
static float dequant(int32_t raw,const hbDNNTensorProperties *p,int point) {
    if(p->quantiType==SHIFT) return ldexpf((float)raw,-(int)p->shift.shiftData[point]);
    if(p->quantiType==SCALE) return raw*p->scale.scaleData[point];
    return (float)raw;
}
static int parse_landmarks(Model *m,const hbDNNRoi *roi,float *points) {
    for(int axis=0;axis<2;axis++) {
        const hbDNNTensorProperties *p=&m->out[axis].properties;
        REQUIRE(p->tensorType==HB_DNN_TENSOR_TYPE_S32 && p->tensorLayout==HB_DNN_LAYOUT_NHWC);
        REQUIRE(p->validShape.numDimensions==4 && p->validShape.dimensionSize[0]==1 && p->validShape.dimensionSize[3]==LANDMARKS);
        int bins=p->validShape.dimensionSize[axis==0?2:1];REQUIRE(bins==32);
        REQUIRE((p->quantiType==SHIFT && p->shift.shiftLen>=LANDMARKS) || (p->quantiType==SCALE && p->scale.scaleLen>=LANDMARKS) || p->quantiType==NONE);
        int step=axis==0?p->alignedShape.dimensionSize[3]:p->alignedShape.dimensionSize[2]*p->alignedShape.dimensionSize[3];
        REQUIRE(((bins-1)*step+LANDMARKS)*4<=p->alignedByteSize);
        const int32_t *v=m->out[axis].sysMem[0].virAddr;
        for(int k=0;k<LANDMARKS;k++) {
            float best=0;int pos=0;
            for(int i=0;i<bins;i++) { float val=dequant(v[i*step+k],p,k);if(val>best){best=val;pos=i;} }
            float diff=0;
            if(pos>0 && pos<bins-1) diff=dequant(v[(pos+1)*step+k],p,k)-dequant(v[(pos-1)*step+k],p,k);
            float offset=diff>0?.25f:(diff<0?-.25f:0);
            float span=(float)(axis==0?roi->right-roi->left+1:roi->bottom-roi->top+1);
            float origin=(float)(axis==0?roi->left:roi->top);
            points[k*3+axis]=(pos+offset+.5f)*span/bins+origin;
            float score=fminf(1,best/2);
            points[k*3+2]=axis==0?score:fminf(points[k*3+2],score);
        }
    }
    return 0;
}
static int roi_from_box(const float *b,hbDNNRoi *r) {
    for(int i=0;i<5;i++) if(!isfinite(b[i])) return 0;
    float cx=(b[0]+b[2])/2,cy=(b[1]+b[3])/2;
    float w=(b[2]-b[0])*1.25f,h=(b[3]-b[1])*1.25f;
    if(w<=0 || h<=0)return 0;
    r->left=(int)fmaxf(0,cx-w/2);r->top=(int)fmaxf(0,cy-h/2);
    r->right=(int)fminf(WIDTH-1,cx+w/2);r->bottom=(int)fminf(HEIGHT-1,cy+h/2);
    r->left+=(r->left&1);r->top+=(r->top&1);r->right-=!(r->right&1);r->bottom-=!(r->bottom&1);
    return r->right-r->left>=16 && r->bottom-r->top>=16;
}
/* The resizer step limit is 262143 (~4x). A 128px landmark input
 * cannot consume a full-frame ROI directly. Keep the whole ROI by using
 * a half-resolution NV12 source only for those large faces. */
static int prepare_half(const hbDNNTensor *src,hbDNNTensor *dst) {
    const uint8_t *y=src->sysMem[0].virAddr,*uv=src->sysMem[1].virAddr;
    uint8_t *dy=dst->sysMem[0].virAddr,*duv=dst->sysMem[1].virAddr;
    for(int r=0;r<HEIGHT/2;r++)for(int c=0;c<WIDTH/2;c++) {
        int k=2*r*WIDTH+2*c;
        dy[r*(WIDTH/2)+c]=(y[k]+y[k+1]+y[k+WIDTH]+y[k+WIDTH+1]+2)/4;
    }
    for(int r=0;r<HEIGHT/4;r++)for(int c=0;c<WIDTH/4;c++)for(int ch=0;ch<2;ch++) {
        int k=2*r*WIDTH+4*c+ch;
        duv[r*(WIDTH/2)+2*c+ch]=(uv[k]+uv[k+2]+uv[k+WIDTH]+uv[k+WIDTH+2]+2)/4;
    }
    SDK(hbSysFlushMem(&dst->sysMem[0],HB_SYS_MEM_CACHE_CLEAN));
    SDK(hbSysFlushMem(&dst->sysMem[1],HB_SYS_MEM_CACHE_CLEAN));
    return 0;
}
static int needs_half(const hbDNNRoi *r) {
    return ((int64_t)(r->right-r->left)*65536+63)/127>262143 ||
           ((int64_t)(r->bottom-r->top)*65536+63)/127>262143;
}
static void release(Model *m) {
    for(int i=0;i<2;i++) if(m->in.sysMem[i].virAddr) hbSysFreeMem(&m->in.sysMem[i]);
    for(int i=0;i<m->outputs;i++) if(m->out[i].sysMem[0].virAddr) hbSysFreeMem(&m->out[i].sysMem[0]);
    if(m->packed) hbDNNRelease(m->packed);
}
int main(int argc,char **argv) {
    if(argc!=3)return 2;
    int fd=dup(STDOUT_FILENO);if(fd<0 || dup2(STDERR_FILENO,STDOUT_FILENO)<0)return 2;
    FILE *wire=fdopen(fd,"wb");if(!wire)return 2;setvbuf(wire,NULL,_IONBF,0);
    Model det={0},lmk={0};hbDNNTensor half={0};int rc=1;
    if(initialize(&det,argv[1],9,WIDTH,HEIGHT) || initialize(&lmk,argv[2],2,128,128))goto done;
    half.properties=det.in.properties;
    half.properties.validShape.dimensionSize[2]=HEIGHT/2;
    half.properties.validShape.dimensionSize[3]=WIDTH/2;
    half.properties.alignedShape=half.properties.validShape;
    half.properties.alignedByteSize/=4;
    if(hbSysAllocCachedMem(&half.sysMem[0],PIXELS/4) ||
       hbSysAllocCachedMem(&half.sysMem[1],PIXELS/8))goto done;
    if(fwrite("X5FD",1,4,wire)!=4)goto done;
    for(;;) {
        size_t n=fread(det.in.sysMem[0].virAddr,1,PIXELS,stdin);
        if(!n && feof(stdin)){rc=0;break;}
        if(n!=PIXELS || fread(det.in.sysMem[1].virAddr,1,PIXELS/2,stdin)!=PIXELS/2)break;
        if(hbSysFlushMem(&det.in.sysMem[0],HB_SYS_MEM_CACHE_CLEAN) || hbSysFlushMem(&det.in.sysMem[1],HB_SYS_MEM_CACHE_CLEAN))break;
        if(infer(&det,&det.in,NULL))break;
        hbDNNTensor *t=&det.out[5];if(t->properties.tensorType!=HB_DNN_TENSOR_TYPE_F32)break;
        float *raw=t->sysMem[0].virAddr;float bytecount=raw[0];
        if(!isfinite(bytecount) || bytecount<0 || bytecount>MAX_FACES*24 || (int)bytecount%24 || bytecount!=(float)(int)bytecount)break;
        int boxes=(int)bytecount/24;if((boxes+1)*24>t->properties.alignedByteSize)break;
        float records[MAX_FACES][5+LANDMARKS*3];uint32_t count=0;
        int half_ready=0;
        for(int i=0;i<boxes;i++) {
            float *b=raw+6*(i+1);hbDNNRoi roi;
            if(b[4]<.1f || !roi_from_box(b,&roi))continue;
            int scaled=needs_half(&roi);
            hbDNNTensor *input=&det.in;
            if(scaled) {
                if(!half_ready && prepare_half(&det.in,&half))goto done;
                half_ready=1;input=&half;
                roi.left=(roi.left/2)&~1;roi.top=(roi.top/2)&~1;
                roi.right=(roi.right/2)|1;roi.bottom=(roi.bottom/2)|1;
            }
            if(infer(&lmk,input,&roi))goto done;
            memcpy(records[count],b,5*sizeof(float));
            if(parse_landmarks(&lmk,&roi,records[count]+5))goto done;
            if(scaled)for(int k=0;k<LANDMARKS;k++) {
                records[count][5+3*k]*=2;
                records[count][6+3*k]*=2;
            }
            count++;
        }
        if(fwrite(&count,4,1,wire)!=1 || fwrite(records,sizeof(records[0]),count,wire)!=count)break;
    }
done:
    for(int i=0;i<2;i++)if(half.sysMem[i].virAddr)hbSysFreeMem(&half.sysMem[i]);
    release(&lmk);release(&det);fclose(wire);return rc;
}
