-- Transactional native-database regression: no test app becomes visible.
BEGIN;
DO $test$
DECLARE aid bigint; n integer; value text;
BEGIN
 -- is_uninstall/is_stop are capabilities shown by the app center, not state.
 INSERT INTO public.app(app_name,status,is_uninstall,is_stop)
  VALUES('fnos.home-policy-owned-probe','running',true,true) RETURNING id INTO aid;
 SELECT count(*) INTO n FROM public.app_env WHERE app_id=aid AND k IN ('LD_PRELOAD','FNOS_HOME_DATA_ROOT') AND length(v)>0;
 IF n<>2 THEN RAISE EXCEPTION 'new application missed policy'; END IF;
 INSERT INTO public.app_env(app_id,k,v) VALUES(aid,'FNOS_PROBE_KEEP','opaque-owned-fixture');
 DELETE FROM public.app_env WHERE app_id=aid AND k='FNOS_HOME_DATA_ROOT';
 UPDATE public.app SET status='stopped' WHERE id=aid;
 SELECT count(*) INTO n FROM public.app_env WHERE app_id=aid AND k='FNOS_HOME_DATA_ROOT';
 IF n<>1 THEN RAISE EXCEPTION 'lifecycle did not restore policy'; END IF;
 SELECT v INTO value FROM public.app_env WHERE app_id=aid AND k='FNOS_PROBE_KEEP';
 IF value<>'opaque-owned-fixture' THEN RAISE EXCEPTION 'unrelated app setting changed'; END IF;
 UPDATE public.app_env SET v='existing-application-setting' WHERE app_id=aid AND k='LD_PRELOAD';
 UPDATE public.app SET status='running' WHERE id=aid;
 SELECT v INTO value FROM public.app_env WHERE app_id=aid AND k='LD_PRELOAD';
 IF value<>'existing-application-setting' THEN RAISE EXCEPTION 'existing app preload overwritten'; END IF;
 RAISE NOTICE 'PASS: new-app policy, lifecycle repair, unrelated setting preservation, conflict preservation';
END $test$;
ROLLBACK;
