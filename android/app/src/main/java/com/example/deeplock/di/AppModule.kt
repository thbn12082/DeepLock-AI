package com.example.deeplock.di

import android.content.Context
import androidx.room.Room
import com.example.deeplock.data.local.DeepLockDatabase
import com.example.deeplock.data.local.LockCycleDao
import com.example.deeplock.data.local.QuizSessionDao
import com.example.deeplock.data.local.StudyDao
import com.example.deeplock.data.local.WidgetSnapshotDao
import dagger.Module
import dagger.Provides
import dagger.hilt.InstallIn
import dagger.hilt.android.qualifiers.ApplicationContext
import dagger.hilt.components.SingletonComponent
import javax.inject.Singleton

@Module
@InstallIn(SingletonComponent::class)
object AppModule {
    @Provides @Singleton
    fun database(@ApplicationContext context: Context): DeepLockDatabase =
        Room.databaseBuilder(context, DeepLockDatabase::class.java, "deeplock.db")
            .addMigrations(
                DeepLockDatabase.MIGRATION_1_2,
                DeepLockDatabase.MIGRATION_2_3,
                DeepLockDatabase.MIGRATION_3_4,
            )
            .build()

    @Provides fun studyDao(database: DeepLockDatabase): StudyDao = database.studyDao()
    @Provides fun lockCycleDao(database: DeepLockDatabase): LockCycleDao = database.lockCycleDao()
    @Provides fun quizSessionDao(database: DeepLockDatabase): QuizSessionDao = database.quizSessionDao()
    @Provides fun widgetSnapshotDao(database: DeepLockDatabase): WidgetSnapshotDao = database.widgetSnapshotDao()
}
