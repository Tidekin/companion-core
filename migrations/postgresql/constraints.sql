-- Native derived lexical index; PostgreSQL maintains eligibility on update/delete.
CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;
CREATE INDEX idx_memories_lexical ON memories USING gin (content public.gin_trgm_ops)
    WHERE status IN ('CONFIRMED','HYPOTHESIS') AND privacy_scope='private' AND confidence>0;

CREATE FUNCTION enforce_goal_step_limit() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    step_count bigint;
BEGIN
    -- Lock parents in a stable order. A no-op write also makes stale repeatable-read
    -- writers fail instead of admitting a fourth child from an older snapshot.
    IF TG_OP = 'UPDATE' THEN
        PERFORM goal_id FROM goals WHERE goal_id IN (NEW.goal_id,OLD.goal_id)
            ORDER BY goal_id FOR UPDATE;
    ELSE
        PERFORM goal_id FROM goals WHERE goal_id=NEW.goal_id FOR UPDATE;
    END IF;
    UPDATE goals SET version=version WHERE goal_id=NEW.goal_id;
    IF TG_OP = 'UPDATE' THEN
        SELECT count(*) INTO step_count FROM goal_steps
            WHERE goal_id=NEW.goal_id AND step_id<>OLD.step_id;
    ELSE
        SELECT count(*) INTO step_count FROM goal_steps WHERE goal_id=NEW.goal_id;
    END IF;
    IF step_count >= 3 THEN
        RAISE EXCEPTION 'GOAL_STEP_LIMIT' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER goal_steps_max_three BEFORE INSERT OR UPDATE OF goal_id ON goal_steps
    FOR EACH ROW EXECUTE FUNCTION enforce_goal_step_limit();

CREATE FUNCTION invalidate_memory_vector() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    DELETE FROM memory_vectors WHERE memory_id=NEW.memory_id;
    RETURN NEW;
END;
$$;
CREATE TRIGGER memory_vector_invalidate AFTER UPDATE ON memories
    FOR EACH ROW WHEN (NEW.content IS DISTINCT FROM OLD.content OR
        NEW.status NOT IN ('CONFIRMED','HYPOTHESIS') OR NEW.confidence<=0)
    EXECUTE FUNCTION invalidate_memory_vector();
